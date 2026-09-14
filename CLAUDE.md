# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository state: specification only

This repository currently contains **no source code**. It holds one design document:

- `README.md`
- `PAPER_AGENT_SPEC.md`

These two files are **byte-identical copies** of the same 1749-line specification. There is no
package manifest, no build system, no test suite, no CI, and no dependency lockfile.

Two consequences:

1. **Do not hunt for an implementation** — there isn't one. The spec describes a system to be
   built, not a system that exists. Descriptions in it are requirements, not documentation of
   working behavior.
2. **Any spec edit must be applied to both files**, or they silently drift. Verify with
   `diff README.md PAPER_AGENT_SPEC.md` before committing. (If the user wants one to become the
   canonical copy and the other a pointer, that is a reasonable change to propose — but make it
   deliberately, not as a side effect.)

### Commands

There are none to document yet. When the first code lands, establish the run/test/lint commands
in the same change and record them here. Do **not** invent or guess commands for tooling that
isn't in the repo.

The spec's code samples are Python, and it names MT5 as the broker, Telethon for Telegram history
backfill, and YAML for config — so a Python project is implied. Treat that as the spec's
expectation, not a decision already made; confirm the stack with the user before scaffolding.

## What the system is

An autonomous XAUUSD (gold) trading system: it ingests signals from Telegram (text and
screenshots), extracts structured trade instructions, validates them, correlates follow-up
messages to open positions, applies risk policy, and executes via a broker API — with no human
in the approval path.

The governing principle, from which nearly every rule in the spec derives:

> When uncertainty exists, prefer missing a trade over entering the wrong trade, and prefer
> reducing risk over increasing risk.

A missed trade is the intended behavior, not a bug. Expect to write code that rejects far more
than it accepts.

## Pipeline architecture

The spec mandates these as **separate layers**, each independently testable (spec §37, §41):

```text
Telegram sources → Archiver → Parser (text + vision) → Normalizer → Validator
  → Correlator → Policy Engine → Risk Engine → Execution Engine → Broker/MT5
```

Layer boundaries that must not be crossed (spec §44):

- No risk logic in the parser.
- No parser logic in the execution layer.
- Nothing bypasses the Policy Engine.
- Not one large Python file.

Four distinct rejection concerns, deliberately kept apart (spec §41) — conflating them is a
design error, not a simplification:

| Concern | Question |
| --- | --- |
| Signal validity | Is this a well-formed BUY/SELL setup? |
| Trading eligibility | Is the bot allowed to trade right now? |
| Risk eligibility | Is this trade within risk limits? |
| Execution validity | Can the broker safely execute it? |

A perfectly valid signal rejected because the morning session is disabled is correct behavior.

Every signal walks a lifecycle (spec §25): `RAW → PARSED → NORMALIZED → VALIDATED → CORRELATED
→ POLICY_CHECKED → EXECUTION_APPROVED → SUBMITTED → BROKER_CONFIRMED → MANAGED → CLOSED`. Any
stage may emit `REJECTED` — always with a structured reason code from the spec's enumerated list
(spec §26), never a bare "signal rejected".

## Hard invariants — never weaken these

These are enforcement requirements, not conventions. The spec explicitly forbids removing them
to increase trade count (spec §42). If a change would relax one, stop and raise it with the user
rather than implementing it.

- **`MAX_CONCURRENT_POSITIONS = 1`.** Enforced in the execution layer. If a position exists, a
  new order is rejected outright — do not reason about whether the two are correlated. This
  invariant is also what makes single-position correlation fallback safe (spec §3.1, §18).
- **Entry confidence < 0.95 → skip.** Entry decisions are asymmetric: a missed entry costs
  nothing (spec §9).
- **Image double-parse consensus.** Every image is parsed twice with independent instructions.
  On disagreement in any critical field (direction, entry, SL, TP, TP1, TP2, symbol): **reject**.
  Never average the numbers, never pick the "more reasonable" parse, never add a third tiebreaker
  heuristic (spec §11).
- **No naked orders.** Every position is submitted with a valid SL. A position is not considered
  protected until the broker confirms the SL (spec §20).
- **Daily loss kill switch** persists across restart, crash, reconnect, and deploy, and requires
  **manual re-arm**. A process restart must never re-enable trading (spec §21).
- **Order rate limit: 1 new order / 60 seconds**, as a runaway guard independent of the parser
  (spec §24).
- **Orphan sweeper** every ~60s: reconcile broker positions against the internal DB; a broker
  position with no internal record is flagged and handled. No unknown position is left unmanaged
  (spec §22).
- **Deduplication** on `source_message_id` + `content_hash` + dedupe window, covering reposts,
  edits, network/API retries, and restarts (spec §23).

## Session and daily-state rules

All times are **`Asia/Kolkata` (IST)** with a timezone-aware clock. Naive local timestamps are a
bug (spec §4).

- Morning entries: `05:30–09:00 IST` only. Outside the window, existing positions are still
  managed; session close never force-closes a position unless a separate risk policy says so.
- Evening session opens `20:00 IST`, **max 1 new trade**, closing time configurable. "After 8 PM"
  does not mean all-night trading.
- Evening conditional trade requires `successful_70pip_trades_today >= 2`, and caps SL/risk at
  **40 pips**. Passing this gate does not make the signal valid — it still runs every other check
  (spec §7).
- `consecutive_losing_trades >= 3` disables **morning** new entries. The evening cap stays at 1 —
  evening is not an unlimited recovery window (spec §8).
- A "successful 70+ pip trade" means a *realized* winner with validated movement ≥ 70 pips.
  Touching +70 and then losing does not count; reaching TP1 alone does not count (spec §6).

`DailyState` (date, morning/evening trade counts, win/loss counts, `consecutive_losses`,
`successful_70pip_trades`, `morning_disabled`, `evening_trade_consumed`, `daily_kill_switch`)
must survive process restarts — never RAM-only (spec §5).

Startup order is fixed, and **trading stays disabled throughout reconciliation**: connect DB →
connect broker → fetch open positions → fetch recent orders/deals → reconcile → detect orphans →
restore daily state → restore kill switch → verify symbol config → *only then* enable signal
processing (spec §36).

## Broker and price mechanics

- **Never assume `1 pip == 1 point`.** Verify against the broker's XAUUSD specification (spec §13).
- All pip/point/spread/slippage/stop-distance math lives in **one central price utility module**.
  Broker-specific pip assumptions must not be scattered through the codebase.
- Position sizing and partial closes must normalize to broker `min_volume` / `max_volume` /
  `volume_step`. If a partial close can't execute safely, use a deterministic fallback — never
  silently over-close and never silently increase risk (spec §17).
- Protective SL after TP1 is entry ± (slippage + spread + 15 pips), but the implementation must
  account for **which side of the spread closes the position** rather than mirroring one formula
  across BUY and SELL. The resulting SL has to actually lock in the intended protection (spec §14).
- TP management: TP1 closes 50%, then SL moves to the protective level. For large targets
  (≥ 170 pips), TP2 closes **50% of the remaining volume**, not another 50% of the original
  (spec §15, §16).

## Correlating follow-up messages

Follow-ups like "move SL to BE", "close the sell", "book half" resolve deterministically by
priority: reply-to message ID (1.0) → unique price match within 50 pips (0.90) → unique direction
match (0.85) → single-open-position fallback (0.80) → otherwise no match (spec §18).

Ambiguity is resolved **by the direction of risk**, not by confidence alone (spec §19):

- Protective/risk-reducing action (move to BE, close, partial close) + exactly one open position
  + unambiguous action → apply it.
- Risk-increasing action (widen SL, add position, increase size, remove SL, move SL away from
  price) under any uncertainty → **ignore**. Never guess.

## Build order

The spec prescribes a strict sequence (spec §28, §43) — the archiver is first, and full execution
is explicitly deferred:

`P0` Archiver (Telegram ingestion, history backfill, raw + image storage, reply graph, dedupe) →
`P1` hand-labeled dataset + parser + evaluation → `P2` Correlator → `P3` Policy Engine →
`P4` Risk Engine → `P5` Shadow execution (`WOULD ENTER` / `WOULD MODIFY` / `WOULD CLOSE`, no
orders sent) → `P6` Demo account, minimum 2 weeks → `P7` Live at minimum position size.

Do not develop the parser against invented examples — build the real corpus first.

**Live gate** (spec §31), a minimum bar rather than a target: zero direction errors across 100
consecutive validated signals, **and** ≥ 98% field-level accuracy on entry/SL. Failing the gate
means full auto stays off.

## Working in this repo

- The spec is the authority. Its section numbers are stable — cite them (e.g. "spec §14") when
  explaining a decision, and read the relevant section before changing behavior it covers.
- Safety-critical constants belong in one central config (the spec sketches a YAML layout in
  §38). Do not duplicate thresholds across modules.
- Every decision must be reconstructible after the fact: log the raw signal, both parser results
  and confidences, validation outcome, market price and spread, computed SL distance and risk,
  daily state, session, policy and correlation results, execution result, broker ticket, and
  position state. The logs must answer "why did the bot take this trade?" and "why was this
  signal rejected?" without guesswork (spec §27).
- Every safety rule needs a test. The spec enumerates required coverage per layer in §39,
  including session boundary cases (05:29/05:30, 08:59/09:00, 19:59/20:00) and recovery cases
  (restart with an open position, orphan position, missing DB record, broker timeout, duplicate
  execution request).
- Prefer deterministic logic over model judgment, and never increase risk because a model
  "thinks" it is probably correct (spec §44).
