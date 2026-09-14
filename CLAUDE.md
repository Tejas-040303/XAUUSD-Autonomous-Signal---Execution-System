# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# XAUUSD BOT — ENGINEERING RULES

`README.md` (spec §1–§46) is the authority. Cite sections (e.g. "spec §14") when explaining a
decision, and read the relevant section before changing behaviour it covers. Where this file and
the spec disagree, the disagreement is called out explicitly below — do not resolve it silently.

## Critical invariants

Enforcement requirements, not conventions. Spec §42 forbids removing any of them to increase
trade count. If a change would relax one, **stop and raise it** rather than implementing it.

1. `MAX_CONCURRENT_POSITIONS = 1`. Enforced in the execution layer. If a position exists, a new
   order is rejected outright — never reason about whether the two are correlated.
2. Every position must have an SL. Not protected until the **broker confirms** it (spec §20).
3. Never increase risk because of ambiguity.
4. Never silently guess financial instructions.
5. Entry confidence requires **full parser consensus plus all deterministic validation
   passing**. There is no numeric threshold — see Confidence below.
6. Image parser disagreement means rejection. Never average, never prefer the "more reasonable"
   parse, never add a third tiebreaker heuristic (spec §11).
7. Daily kill switch requires manual re-arm. Survives restart, crash, reconnect, deploy — and
   **does not clear at midnight**.
8. Broker state is authoritative for live positions. The local DB is a cache that can be stale
   or wrong, especially after a crash.
9. Never bypass the policy engine.
10. Never modify execution safety rules without explicit approval.

## Never

- Never enter without SL.
- Never open a second position.
- Never retry an unknown broker order blindly — resolve it against broker truth.
- Never treat temporary +70 pip movement as a successful 70-pip trade.
- Never use AI judgment to override deterministic safety rules.
- Never post status messages into the channel the bot reads signals from (feedback loop: the
  bot's own message quoting a price can be re-ingested as a signal). Separate chat, **and** an
  ingestion filter on our own sender id.
- Never let untrusted message content reach a model outside a delimited, length-bounded data
  block. Signals are attacker-controllable text and images from a channel we do not control.

## Development rules

- Inspect before modifying.
- Prefer small commits.
- Write tests with every safety-critical feature.
- Never delete existing functionality without proving it is obsolete.
- Never fabricate broker/API behaviour. When MT5 or Telegram behaviour is unclear, write the
  adapter against the interface, stub it, and leave a `# VERIFY:` comment naming the doc page a
  human must check. Do not guess endpoint names, parameter names, retcode numbers, or response
  shapes.
- Use timezone-aware datetime. **Store UTC**; one `clock.now_utc()` is the only time source;
  `now_ist()` derives from it for session logic only. Tests inject a fake clock. A lint test
  asserts no other module imports `datetime.now`.
- All money/risk calculations must be deterministic.
- All broker price calculations must go through centralized utilities (`PriceUtils`).
- Never hard-code pip/point assumptions throughout the codebase. No module outside `price.py`
  may contain a numeric price constant.
- Ledger before broker, always: an `intents` row with its idempotency key is committed **before**
  any request leaves the process. This ordering is what makes crash recovery decidable.
- Every rejection carries a structured reason code from spec §26 — never a bare
  "signal rejected". Every decision must be reconstructible afterwards from the `decisions`
  table alone (spec §27).

## Risk limits

```text
Account basis              $1,000
daily_loss_limit           $50            (5% of starting-day equity)
max_sl_pips                70             REJECT above — do not clamp, do not trade it
min_rr                     1:1            measured entry → FINAL TP (see note)
max_daily_trades_morning   3
max_daily_trades_evening   1              (spec §7)
max_sl_pips_evening        40             (spec §7, stricter than the morning cap)
```

`max_sl_pips = 70` comes from the observed signal source: it never posts more than 60–70 pips.
A signal above 70 is **rejected**, never clamped to fit — clamping would silently change the
trade the provider specified.

**`min_rr` is measured against the final TP, not TP1.** Measured against TP1 it would reject
most real signals: a 30-pip TP1 with a 60-pip SL is 0.5:1. Flagged because the choice is not
obvious and it changes which signals survive.

**Position sizing for this account: fixed minimum volume, not risk-percentage.** Four losses
(3 morning + 1 evening) must fit inside `$50`, so per-trade risk must be `<= $12.50`. Risk-based
sizing also has a known failure mode — a tighter SL buys a *larger* lot, so the worst-case trade
is the one with the least room. At `0.01` lots a 70-pip SL risks about `$7` (pip = `$0.10`),
which fits; `0.02` lots risks `$14`, which does not. This matches §34's "start with minimum
practical position size".

**The kill switch is an entry ban, not a loss cap (spec §21).** It stops new entries; §4 forbids
force-closing, so an open position runs to its stop. Real worst case is therefore
`daily_loss_limit + one full max_sl trade + gap slippage`. Trip the switch at
`daily_loss_limit − max_open_risk` if it is to actually bind, and decide explicitly whether
tripping flattens the open position.

## Trading rules

```text
Max concurrent positions   1
Order rate limit           1 new ENTRY / 60s
                           (modifications, closes, partial closes NOT throttled)
```

### Morning session

```text
New entries        05:30 – 09:00 IST
Max new trades     3 per morning  (morning_trades)
Max SL / risk      70 pips
Outside window     NO NEW ENTRY — existing positions are still managed
Session close      never force-closes a position unless a risk policy says so (spec §4)
```

**Three consecutive losses** → morning new entries DISABLED. The losses must be consecutive; a
winning trade resets the counter to 0.

The 3-trade cap closes a real gap: spec §5 tracks `morning_trades` and **no rule in §1–§46 ever
consumes it**. §7's cap of 1 is evening-only, so the morning window was uncapped — 210 minutes
against a 60-second order limiter. A trade count is now the binding constraint alongside
`daily_loss_limit`.

> Note on the 3-loss reset: §8 says "a winning trade resets `consecutive_losses = 0`" with no
> minimum size, so a +0.3 pip scratch resets the brake. Worth deciding whether a reset should
> require a win above some threshold, or whether the rule should be `losses_today >= 3` rather
> than *consecutive*. Not changed here — raising it, per invariant 10.

### Evening session

```text
New entries        from 20:00 IST
End time           configurable (spec §4) — REQUIRED config, no default
Max new trades     1 per evening  (evening_trade_consumed)
Max SL / risk      40 pips
Precondition       successful_70pip_trades_today >= 2
```

Even with morning disabled, the evening remains **MAX 1 NEW TRADE**. It is not an unlimited
recovery opportunity (spec §8).

Passing the evening precondition does **not** make a signal tradeable — *trade eligibility* and
*signal validity* are separate (spec §7, §41). The signal still runs every parser, confidence,
price, SL, TP, spread, risk and execution check.

A "successful 70+ pip trade" is a *realized* winner with validated movement >= 70 pips.
Touching +70 then losing does not count. Reaching TP1 alone does not count (spec §6).

**The two wins are counted separately — 70 and 70, never summed.** The gate needs *two distinct
trades*, each independently reaching >= 70 pips. One trade that moves 140 pips is **one**
success, not two. Two trades of 35 pips each are **zero** successes, not one. Cumulative pips
across trades are never added together for this gate.

`end` has no invented default: an unbounded overnight session is the failure spec §4 warns
against ("do not assume that after 8 PM means the bot can continuously trade all night"), so
startup fails closed until a value is set.

### TP ladder — driven by target distance (spec §15, §16)

```text
Standard target  (< 170 pips)
  TP1        → close 50%
  Final TP   → close remaining 50%

Large target    (>= 170 pips)
  TP1        → close 50%
  TP2        → close 50% OF REMAINING   (25% of original)
  Final TP   → close remainder          (25% of original)
```

TP2 is **50% of remaining volume**, never another 50% of the original. Worked example on 2.00
lots: TP1 closes 1.00 (1.00 left) → TP2 closes 0.50 (0.50 left) → final TP closes 0.50.

TP1 *distance* comes from the signal (30 / 35 / 60 / 75+ pips, market dependent) and is **not** a
configured constant. The 170-pip threshold classifies the trade; it does not set any TP level.

### Staged-exit eligibility — checked BEFORE entry

The 170-pip threshold says what ladder the trade *wants*. This gate says whether a staged exit is
physically **placeable**, and it is evaluated at approval time, not discovered at TP1:

```text
staged_exit_eligible  ⟺  TP1_distance >= spread_budget
                                       + expected_slippage
                                       + protective_buffer (15 pips)
                                       + broker stops_level
                                       + margin
```

If ineligible, the signal is a **single-exit trade by design**: full volume, broker-side SL and
TP, no partial legs. Decided deterministically up front and logged as such.

Why this exists: without it, any signal whose TP1 is closer than that sum degenerates to "close
everything at TP1" *via a rejection cascade* — the protective SL is unplaceable, the fallback
ladder below descends to rung 3, and the runner is closed. Systematically, invisibly, and logged
as a fallback rather than as a decision. With TP1 as tight as 30 pips and gold spreads widening
at rollover, this is the common case, not an edge case.

Also assert at approval time that `TP1 < protective_SL_level` can never hold. If it does, either
reject the signal or classify it single-exit — never enter a trade whose first target sits behind
the stop it will move to.

**Unresolved, ask before coding:**

- "Target distance" is assumed to mean **entry → final TP**. Spec §16 does not say which target
  it measures. If it means entry → TP1, the classification changes for most signals.
- **Large target but only 2 TPs in the signal** — the 3-stage ladder has no third level to exit
  at. Assumed: degrade to `50% / 50%` at the two levels given. Do not synthesise a third level.
- **Standard target but 3 TPs in the signal** — assumed: honour the signal's levels
  (`50% / 25% / 25%`), since exiting requires a level the signal actually specifies.
- `1 TP` → single full exit, no partial ladder. `4+ TPs` → **reject**
  (`TP_COUNT_UNSUPPORTED`); do not guess a ladder. Non-monotonic or wrong-side TPs → reject.

### After TP1 — protective SL

Apply a protected SL using broker-aware spread/slippage calculation. Account for **which side of
the spread closes the position** — do not mirror one formula across BUY and SELL (spec §14).

The computed level can be unplaceable: with TP1 as tight as 30 pips and spread blown out to 30
pips at rollover, `entry + spread + slippage + 15` lands above the current bid and the broker
rejects it. Deterministic fallback ladder, each rung less protective than the last, never more
risky:

```text
1. protective level   entry ± (spread + slippage + 15 pips)
2. breakeven ± broker min_stop_distance
3. close the remaining volume entirely
   → log which rung fired, notify; never silently skip
```

### Position reporting

While a position is open, post a status update every **3 minutes**, plus an event message on
every transition: `SL_HIT`, `BE_MOVED`, `TP1_FILLED`, `TP2_FILLED`, `FINAL_TP`, and every
rejection with the full broker retcode, constant name and message. Delivered via a DB outbox
drained by a separate loop — a Telegram outage must never block or delay execution.

The outbox exists (`xauusd/notify/outbox.py`). Three properties to preserve when the execution
engine starts using it:

- **Enqueue inside the caller's transaction.** "We moved the stop" and "we said we moved the stop"
  commit together or not at all.
- **Priority, not insertion order.** After a ten-minute outage, a queue ordered purely by arrival
  delivers two hundred stale heartbeats before the `SL_HIT` that happened during it. An operator
  told about a stop forty minutes late has been actively misled.
- **Heartbeats collapse, events never.** A heartbeat is a snapshot, so an older pending one carries
  no information once a newer exists; it is replaced in place, keeping its queue position. Event
  messages carry no collapse key — losing one loses the record.

Delivery is **at-least-once**: the Bot API has no client-supplied idempotency key, so a crash
between sending and recording resolves toward a duplicate. A repeated `SL_HIT` notice is noise; a
missing one is an operator who does not know their stop was hit. Terminally undelivered messages
are **kept, never dropped** — an undelivered `SL_HIT` is evidence about the incident — and surfaced
at startup.

Run **exactly one drainer**. Two would each claim disjoint rows but could deliver out of order, and
`TP1_FILLED` arriving after `FINAL_TP` reads as a different trade.

## Archive and duplicate detection

The archive is **evidence**, not a cache. It answers "what exactly did the provider post, and
when did we see it" months later — including for a message since edited or deleted. So nothing in
it is ever destructively updated or deleted:

- an **edit** appends a revision to `message_edits`; `messages.text` stays as first posted, because
  the SL we actually traded has to stay provable when the provider changes it afterwards;
- a **deletion** is recorded in `message_deletions` and the row stays. A provider removing a
  losing call is exactly what the archive exists to remember.

### `content_hash` — specified here, undefined in the spec

Spec §23 requires duplicate handling but never says what makes two messages "the same". The
definition:

```text
content_hash = sha256(canonical_json({
    "v":     HASH_VERSION,
    "text":  normalized_text,          # NFKC, strip Unicode Cf + Cc, collapse ws, casefold
    "media": [sha256 of each attachment, in order],
}))
```

**Content hashing never decides whether a message is archived.** The archive is keyed on
`(chat_id, message_id)`, which is the only uniqueness it enforces. The hash is a *lead* for the
signal layer, stored beside the message, indexed and never constrained.

That separation is load-bearing. The obvious implementation — hash the text, make it unique —
destroys the corpus §28 depends on: every image-only message has empty text, so they all hash
alike and every screenshot after the first is discarded as a duplicate. The screenshots *are* the
corpus. Hence:

- media contributes its **bytes** to the hash, so two different screenshots never collide and two
  identical ones always do;
- a message with neither text nor media hashes to **NULL**, not `sha256("")`. "Cannot be compared"
  is not "equal to every other empty thing", and a NULL deliberately matches nothing in SQL, so
  the fail-closed behaviour is automatic at the query layer;
- normalisation strips Unicode `Cf` (zero-width space, joiner, BOM, bidi marks). These are
  invisible, so a single injected `U+200B` would otherwise produce a "new" signal that renders
  identically to one already acted on — a duplicate trade for free, invisible in review;
- emoji **survive**. A red circle against a green one may be the only thing distinguishing a sell
  from a buy in this channel's format.

The rule is versioned. A stored hash is comparable only to another of the same `HASH_VERSION`.

### Unresolved media is not "no duplicate"

`content_state` is one of `complete` / `pending_media` / `media_failed`, and the hash is set only
once every attachment is terminal:

| media state | contributes | message identity |
|---|---|---|
| stored | its `sha256` | computable |
| rejected by policy (wrong type, too large, bomb) | a stable namespaced token | computable |
| pending / retryable failure | nothing | **NULL — not comparable** |

A policy rejection is *known* content — we can say exactly what it was and why, identically on
every re-scan — so it does not block the fingerprint, and a text signal carrying a stray PDF stays
comparable. A fetch *failure* is unknown, so it does. A message with a NULL hash must never be
judged a duplicate, and the signal layer must equally refuse to act on one: we cannot prove it is
not a repeat.

### Coverage is intervals, not a watermark

Telegram message ids are **not contiguous** — deletions and service messages consume them — so
"id 4243 is absent from the archive" does not mean it was missed. And a high-water mark cannot
express a hole: backfill walks history *backwards*, so an interrupted run leaves the newest block
present and an older block missing, and a watermark reports "scanned to the newest", which is true
and useless.

So `scan_ranges` stores closed intervals actually looked at, merged and non-overlapping. The
complement within a window is the work still to do. **Scanned-and-absent is thereby distinguished
from never-looked-at**, which is the distinction the whole design turns on.

`max_scanned()` is reporting only — never a resume point. Every coverage question goes through
`gaps()`.

### The ordering that makes a crash survivable

**Messages are recorded before their range is marked scanned, always.** Separate transactions, so
a crash between them re-scans a window already archived — harmless, because every archive write is
idempotent. The opposite order marks coverage for messages never stored, and nothing looks at that
range again. One direction costs duplicate work; the other loses signals permanently and silently.

### Media is hostile input

Three defences, in order: admission on Telegram's declared MIME/size/dimensions **before any bytes
transfer** (so a 900-megapixel bomb costs no bandwidth and a posted `.exe` is never fetched);
paths built **only** from ids we assign, with the extension from a MIME allowlist, so
`DocumentAttributeFilename.file_name` — attacker-controlled text — cannot become a traversal or a
Windows reserved-device bug; and verification of the actual bytes afterwards, because the
declaration was only ever a claim.

Declared dimensions reduce the attack surface, they do not close it: a JPEG header can lie, and
the real pixel count is known only to a decoder. **P1 must re-assert `max_image_pixels` against
the decoded image**, which is the only place that truth exists.

## Order state machine

`UNKNOWN` is an **order-request** outcome, not a trade outcome. SL/BE/TP describe a position that
definitely exists; `UNKNOWN` means we do not know whether one exists at all (request sent, no
reply). Treating `submit()` as a boolean, or defaulting `UNKNOWN` to `REJECTED` and retrying, is
how you end up with two positions and double risk.

```text
PENDING → SUBMITTED → ACKED → PARTIAL → FILLED
                    ↘ REJECTED / CANCELLED
                    ↘ UNKNOWN  ← MUST reconcile against broker truth, never blind retry
```

## Reconciliation

Two paths, both reading broker truth rather than the local DB:

- **Startup** (spec §36): connect DB → connect broker → fetch positions → fetch recent
  orders/deals → reconcile → detect orphans → restore daily state → restore kill switch →
  verify symbol config → *only then* enable signal processing. Trading stays disabled throughout.
- **Sweeper** (spec §22, ~60s). Spec §22 describes only one of four cases; handle all four:

| At broker | In DB | Meaning | Action |
|---|---|---|---|
| exists | exists, volume matches | healthy | none |
| exists | absent | orphan | adopt if our `magic`; else alert + policy close |
| absent | open | phantom (closed at broker, DB stale) | find closing deal, mark closed, fix daily state |
| exists | exists, volume differs | partial-close desync | broker volume is truth; repair DB, re-verify SL |

The phantom case is a *correctness* issue beyond safety: a win the DB never learned about leaves
`consecutive_losses` and `successful_70pip_trades` wrong, silently corrupting the rules above.

## State persistence

`DailyState` (date_ist, morning/evening trade counts, wins, losses, `consecutive_losses`,
`successful_70pip_trades`, `morning_disabled`, `evening_consumed`) must survive restarts — never
RAM-only (spec §5). A row whose `date_ist` is not today is never loaded as today's state.

The **kill switch lives outside `DailyState`**, in a singleton `bot_state` table. Spec §5 places
it inside date-keyed `DailyState`, which would re-arm it at midnight and violate §21. This file
overrides the spec here.

## Pipeline architecture

Separate layers, each independently testable (spec §37, §41):

```text
Telegram → Archiver → Parser (VLM + OCR) → Normalizer → Validator
  → Correlator → Policy Engine → Risk Engine → Execution Engine → Broker/MT5
```

Boundaries that must not be crossed (spec §44): no risk logic in the parser; no parser logic in
execution; nothing bypasses the policy engine; not one large Python file. Enforce mechanically —
one test asserts only `parse/vlm.py` imports the Anthropic SDK, and only `broker/mt5.py` imports
`MetaTrader5`. Comments do not hold.

Four rejection concerns, deliberately kept apart (spec §41) — conflating them is a design error:

| Concern | Question | Layer |
|---|---|---|
| Signal validity | Is this a well-formed BUY/SELL setup? | validator |
| Trading eligibility | Is the bot allowed to trade right now? | policy |
| Risk eligibility | Is this trade within risk limits? | risk |
| Execution validity | Can the broker safely execute it? | execution |

A valid signal rejected because the morning session is disabled is **correct behaviour**, not a
bug. Expect code that rejects far more than it accepts.

`policy` and `risk` are pure functions over `(signal, daily_state, clock, config)` — no I/O, no
broker, no model. That is what makes spec §39's exhaustive rule testing cheap.

## Confidence

Confidence is **computed, never asked for**. A model that reports its own confidence is reporting
a number an injected instruction can set, and §44 forbids increasing risk because a model thinks
it is probably correct.

```text
both parsers agree on every critical field
  AND all deterministic validation passes        → proceed
anything else                                    → REJECT
```

Critical fields are direction, entry, SL, and every TP level (spec §11).

**There is no numeric confidence threshold.** §38's `minimum_entry_confidence: 0.95` is deleted:
with a binary outcome every threshold in `(0, 1]` behaves identically, so it was a safety
constant that could not change behaviour while appearing to. Do not reintroduce it. §9's intent
is preserved exactly — the bar is total agreement, which is stricter than 0.95, not looser.

If a graded score is ever wanted, it must come from **measured per-field parser accuracy** on the
labelled corpus, never from the model's own output.

Note the boundary this does *not* cover: consensus exists only for images. A text-only signal has
one model in the path, so it needs a deterministic regex/grammar extractor as its second parser —
text is easier to parse deterministically than images, so there is no excuse for skipping it.

## Correlating follow-up messages

Priority (spec §18): reply-to id (1.0) → unique price match within 50 pips (0.90) → unique
direction match (0.85) → single-open-position fallback (0.80) → no match.

Resolved by **direction of risk**, not confidence alone (spec §19):

- Risk-**reducing** (move to BE, close, partial close) at `>= 0.80` with one open position and an
  unambiguous action → apply.
- Risk-**increasing** (widen SL, add position, increase size, remove SL, move SL away from
  price) → requires `reply_to_id` (1.0); otherwise **ignore**. Never guess.

## Build order

`P0` Foundations + Archiver → `P1` Parser + measured accuracy → `P2` Decision engine in shadow
mode → `P3` Execution + position management (demo). Live is a go/no-go decision on P3 evidence,
not a phase. Do not develop the parser against invented examples — build the real corpus first
(spec §28).

**P0 is done.** P1 cannot start until the archiver has actually run against the real channel:
the corpus is its input, and a parser developed against invented screenshots measures nothing.
The two things P1 needs first are a labelled corpus and a decision on the text-only second
parser (see Confidence — a deterministic grammar, not a second model).

**Live gate** (spec §31), a minimum bar not a target: zero direction errors across 100
consecutive validated signals, **and** >= 98% field-level accuracy on entry/SL.

## Unresolved — ask, do not invent

1. **XAUUSD pip definition — now decides whether the account size is viable at all.**
   Spec §13 forbids assuming `1 pip == 1 point`, then §6/§7/§14/§16 state every limit in pips
   without defining one. With the risk limits above, the consequence is no longer abstract.
   XAUUSD contract size is 100 oz/lot, so `0.01` lots is 1 oz and a `$1` price move is `$1`:

   | Convention | 70-pip SL at `0.01` lots | vs `$50` daily limit |
   |---|---|---|
   | pip = `$0.01` | `$0.70` | fine, but a 4-trade day risks `$2.80` — the limit never binds |
   | pip = `$0.10` | `$7.00` | **works** — 4 losses = `$28`, inside the limit |
   | pip = `$1.00` | `$70.00` | **140% of the daily limit on the smallest placeable trade** |

   At `$1.00`/pip the bot must reject every trade, because the minimum volume already exceeds
   the budget. That is "safe" and useless. **Get this from the broker's contract specification
   before anything else.** Do not pick a value.
2. **Deployment platform.** The official `MetaTrader5` Python package is **Windows only** (IPC to
   a running terminal; no Linux build). Unresolved between Windows VPS, a split MQL5-EA + Python
   design, or a broker REST API. Blocks P3, not P0.
3. **Evening after three losses.** Spec §8 sends you to the evening session after 3 consecutive
   losses, but the evening gate requires 2 × 70-pip wins — which 3 losses nearly rules out, so
   §8's remedy is usually unreachable. Intent unconfirmed.
4. **Entry proximity.** Spec §12's `abs(entry − mid)/mid < 0.005` is 0.5% ≈ `$13.25` at gold
   2650 — 3× to 30× the evening 40-pip cap, so it will not catch a stale signal. Replaced by a
   configurable `max_entry_distance_pips`; the value is unchosen.
5. **Evening after three losses** — see item 3. (Confidence semantics resolved: see the
   Confidence section above.)
6. **How 70 pips is measured *within* one trade.** The counting rule is settled (two separate
   trades, never summed). The measurement inside a trade is not. Three readings disagree on the
   normal case: volume-weighted exit distance, maximum favourable excursion plus a winning
   close, or realized cash. Under the weighted reading a fully-won 30/100 two-TP trade realises
   65 pips and **never counts**, which can make §7's evening gate unreachable. Recommended:
   MFE-plus-winner — it matches "the trade moved 70 pips in my favour and I kept some of it",
   and it is what §6's "temporarily reached +70 but subsequently loses does NOT count" is
   carving out. It requires sampling ticks while a position is open. Store the raw inputs and a
   `success_rule_version` so the rule can be re-evaluated over history.
7. **Risk-increasing follow-up actions.** Current rule (below) permits them at `reply_to_id`
   confidence 1.0. Recommendation on the table: **remove them from the action enum entirely**
   for P0–P3, enforced by a lint test. A reply-to link proves a message is *about* a trade; it
   does not prove the sender is authorised to increase risk, and anyone who can post in the
   channel can reply to anything. Code that cannot widen an SL cannot be made to by a bug, a
   misparse, injected screenshot text, or a compromised channel. Awaiting decision.

## Commands

```bash
python3 -m pip install -e '.[dev]'        # install (Python 3.11+)
python3 -m pip install -e '.[telegram]'   # adds telethon; enables the conversion tests
python3 -m pytest                         # full suite
python3 -m pytest tests/test_price.py -k partial_close   # one test or pattern
python3 -m pytest -m property             # property-based tests only
python3 -m pytest -p no:randomly -x       # stop at first failure
```

The archiver:

```bash
python3 -m xauusd.run_archiver --config config/local.yaml check      # no network, no credentials
python3 -m xauusd.run_archiver --config config/local.yaml backfill   # walk history
python3 -m xauusd.run_archiver --config config/local.yaml live       # backfill, then listen
python3 tools/resolve_chat_ids.py --filter VIP   # find a chat_id; channel or group?
```

`check` applies pending migrations; `backfill` and `live` refuse to run against an unmigrated
database unless `--migrate` is passed, so booting never alters the ledger's shape as a side
effect.

If telethon fails to build (`pyaes` + a Debian-patched setuptools raising
`AttributeError: install_layout`), install it inside a venv, which brings its own modern
setuptools.

No linter or formatter is configured yet. Do not add one to a commit that also changes
behaviour — a reformat diff hides the change it travels with.

## Repository state

`README.md` is the spec; `PAPER_AGENT_SPEC.md` is an intentionally frozen reference copy of it —
`README.md` may diverge in future, and that divergence is expected, not drift to be "fixed". Do
not reconcile them.

**P0 is complete.** Nothing trades and no broker code exists; the archiver can connect to
Telegram once credentials and a `chat_id` are supplied.

```text
xauusd/clock.py             the ONLY wall-clock reader; UTC stored, IST derived; iso_utc()
xauusd/price.py             all pip/point/volume/stop arithmetic; owns the pip definition
xauusd/config/schema.py     fail-closed config; every safety value required, no defaults
xauusd/config/secrets.py    the ONLY reader of os.environ; redacts in __repr__
xauusd/db/store.py          connection factory; synchronous=FULL on the trading DB
xauusd/db/migrate.py        versioned migrations in PRAGMA user_version, transactional DDL
xauusd/db/migrations/       archive/001_init.sql, trading/001_init.sql — all STRICT
xauusd/archive/content.py   content_hash: normalisation + fingerprint (pure)
xauusd/archive/ranges.py    coverage interval algebra (pure)
xauusd/archive/media.py     admission policy + safe paths (pure)
xauusd/archive/store.py     idempotent archive writes; no lock held across a network call
xauusd/archive/fetcher.py   download verification, shared by backfill and the live listener
xauusd/telegram/model.py    IncomingMessage, TelegramReader Protocol, FakeReader (pure)
xauusd/telegram/backfill.py the history walk; drives the Protocol, no telethon
xauusd/telegram/ingest.py   the ONLY telethon importer; conversion + live listener
xauusd/notify/outbox.py     transactional outbox: priority, collapse keys, leases
xauusd/run_archiver.py      entry point: check | backfill | live
config/default.yaml.example template; <<< FROM BROKER >>> marks what must be looked up
tools/resolve_chat_ids.py   operator script: resolve a chat_id, classify channel vs group
tests/                      462 tests (477 with the telegram extra installed)
```

Start with `python3 -m xauusd.run_archiver --config config/local.yaml check`. It needs no
credentials and no network: it validates the config, migrates both databases, reports coverage,
and does the pip-viability arithmetic against the daily loss limit — so a wrong `pip_size` is
caught there rather than by every signal being rejected at runtime.

Conventions worth knowing before adding to this:

- **The unresolved pip value does not block writing code, only running it.** `PriceUtils` takes
  a `SymbolSpec` by injection and config requires `pip_size` with no default, so the module is
  complete and fully tested while the system refuses to start until a real value is supplied.
  Apply the same shape to anything else that is undecided: encode it as required config, never
  as a placeholder default.
- **`tests/test_architecture.py` enforces the layer rules with `ast`,** not grep, so an aliased
  import cannot slip past. Rules for modules that do not exist yet are already written there and
  skip until their module lands — add the rule when you plan the module, not after. Live rules:
  one wall-clock reader, one `os.environ` reader, one `isoformat` caller, one importer each of
  `telethon` / `anthropic` / `MetaTrader5`, no I/O in the pure decision layers, and no import from
  ingestion into `policy` / `risk` / `broker` / `execution`.
- **Optional third-party imports go inside the function that needs them.** The `ast` lint still
  sees them wherever the statement sits, but the module stays importable without the extra — which
  is what keeps `test_every_module_is_importable` meaningful, and what will keep the package
  importable on Linux once the Windows-only `MetaTrader5` arrives.
- **`clock.iso_utc()` writes every stored timestamp.** Always UTC, always a literal `Z`, fixed
  width, so lexicographic order is chronological and `BETWEEN` works on the text. Python's
  `isoformat()` renders UTC as `+00:00`; a column holding both spellings breaks range comparisons
  on the boundary silently. Every timestamp `CHECK` in the schema tests for the `Z`, and a lint
  test forbids calling `isoformat` anywhere else. A dedupe window compared against SQLite's own
  `datetime()` — which renders `2026-09-14 05:45:00`, space separator, no `Z`, no microseconds —
  is wrong by hours and fails silently; compute cutoffs in Python.
- **`executescript` commits any open transaction before it runs.** So a migration wrapped in
  `write_transaction` is *not* atomic — the DDL commits and the outer COMMIT finds nothing. The
  transaction has to live inside the script text, which is what `migrate.py` does and what its
  rollback test pins.
- **Never hold a SQLite write lock across a network call.** A download can block for seconds, and
  the outbox is a writer too, so "we archived it" and "we told you" would go quiet together. The
  archive flow is: one transaction to record and decide, no transaction to download, one short
  transaction per result.

> Note on history: commit `8a505a2` ("Update print statement from 'Hello' to 'Goodbye'") actually
> overwrote `PAPER_AGENT_SPEC.md`, which previously held a different 968-line document —
> a "Paper-Trading Research Agent" build spec. It is recoverable via
> `git cat-file blob b4e66502`. Commit messages in this repo do not reliably describe their
> changes; verify against diffs rather than trusting subjects.
