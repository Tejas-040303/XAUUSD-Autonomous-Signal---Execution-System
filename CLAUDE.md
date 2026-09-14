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
5. Entry confidence must be `>= 0.95`.
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

## Trading rules

```text
Morning entries          05:30–09:00 IST
Evening entries          from 20:00 IST — end time is REQUIRED config, no default
Max concurrent positions 1
Order rate limit         1 new ENTRY / 60s (modifications, closes, partial closes NOT throttled)
```

**Three consecutive losses** → disable morning entries. A win resets the counter.

**Two completed winning trades >= 70 pips** → permit one evening trade, subject to all other
rules. Evening SL/risk maximum: **40 pips**.

A "successful 70+ pip trade" is a *realized* winner with validated movement >= 70 pips.
Touching +70 then losing does not count. Reaching TP1 alone does not count (spec §6).

### TP ladder — driven by TP count, not distance

```text
1 TP     → single full exit, no partial ladder
2 TPs    → TP1 close 50%          → TP2 close remaining 50%
3 TPs    → TP1 close 50%          → TP2 close 50% OF REMAINING (25% of original)
                                  → TP3 close remainder
4+ TPs   → REJECT (TP_COUNT_UNSUPPORTED) — do not guess a ladder
```

TP1 distance comes from the signal (30 / 35 / 60 / 75+ pips, market dependent). It is **not** a
configured constant. TP2 is 50% of *remaining* volume, never another 50% of the original.

> **OPEN CONFLICT — do not resolve without asking.** Spec §16 keys the staged exit on
> *distance* (`target >= 170 pips`). The rule above keys it on *TP count*, per explicit
> instruction. The two disagree, and a 2-TP signal with a 200-pip TP2 has no TP3 to stage on.
> The TP-count rule is authoritative; the 170-pip threshold is retained only as a **logged
> label** for analytics. Confirm before changing either.

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

**Live gate** (spec §31), a minimum bar not a target: zero direction errors across 100
consecutive validated signals, **and** >= 98% field-level accuracy on entry/SL.

## Unresolved — ask, do not invent

1. **XAUUSD pip definition.** Spec §13 forbids assuming `1 pip == 1 point`, then §6/§7/§14/§16
   state every limit in pips without defining one. Depending on convention a pip is `$0.01`,
   `$0.10` or `$1.00` — so "70 pips" spans `$0.70` to `$70`. **No threshold is codeable until
   this comes from the broker's contract spec.** Do not pick a value.
2. **Deployment platform.** The official `MetaTrader5` Python package is **Windows only** (IPC to
   a running terminal; no Linux build). Unresolved between Windows VPS, a split MQL5-EA + Python
   design, or a broker REST API. Blocks P3, not P0.
3. **Evening after three losses.** Spec §8 sends you to the evening session after 3 consecutive
   losses, but the evening gate requires 2 × 70-pip wins — which 3 losses nearly rules out, so
   §8's remedy is usually unreachable. Intent unconfirmed.
4. **Entry proximity.** Spec §12's `abs(entry − mid)/mid < 0.005` is 0.5% ≈ `$13.25` at gold
   2650 — 3× to 30× the evening 40-pip cap, so it will not catch a stale signal. Replaced by a
   configurable `max_entry_distance_pips`; the value is unchosen.
5. **Confidence semantics.** §9 gates at `< 0.95` but §11's double-parse is binary, and a
   model's self-reported confidence would contradict §44. Current rule: confidence is *computed*
   — full agreement plus all deterministic validation passing = 1.0, any disagreement = 0.0. A
   graded score would have to come from measured per-field accuracy, never from the model.

## Repository state

No source code yet. `README.md` is the spec; `PAPER_AGENT_SPEC.md` is an intentionally frozen
reference copy of it — `README.md` may diverge in future, and that divergence is expected, not
drift to be "fixed". Do not reconcile them.

There are no build/lint/test commands yet. When the first code lands, establish them in the same
change and record them here. Do **not** invent commands for tooling that is not in the repo.

> Note on history: commit `8a505a2` ("Update print statement from 'Hello' to 'Goodbye'") actually
> overwrote `PAPER_AGENT_SPEC.md`, which previously held a different 968-line document —
> a "Paper-Trading Research Agent" build spec. It is recoverable via
> `git cat-file blob b4e66502`. Commit messages in this repo do not reliably describe their
> changes; verify against diffs rather than trusting subjects.
