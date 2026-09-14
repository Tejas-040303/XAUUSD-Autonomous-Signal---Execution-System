# XAUUSD Autonomous Signal → Execution System

## 1. Project Objective

Build a conservative, production-oriented automated XAUUSD trading system that receives trading signals from external sources, extracts structured trade instructions from text and images, validates those instructions, correlates follow-up messages with existing positions, applies deterministic risk policies, and executes trades automatically.

The system must be designed around one principle:

> **When uncertainty exists, the system must prefer missing a trade over entering the wrong trade, and prefer reducing risk over increasing risk.**

This is an automation-first system.

There must be no assumption that a human will notice a bad signal before execution.

The architecture must therefore treat parsing, correlation, validation, risk management, execution, and position management as separate safety-critical layers.

---

# 2. Core Safety Philosophy

The system must follow these priorities in order:

1. Protect account capital.
2. Prevent incorrect execution.
3. Prevent duplicate execution.
4. Prevent uncontrolled position accumulation.
5. Correctly manage existing positions.
6. Capture valid opportunities.
7. Optimize profitability.

Profitability must never override safety.

A missed trade is acceptable.

A wrong trade is not.

A delayed entry is acceptable.

A naked position is not.

An uncertain risk-increasing action must be rejected.

---

# 3. HARD INVARIANTS

These rules must be enforced in the execution layer.

They must NOT depend on developer discipline, configuration conventions, or prompts.

## 3.1 Maximum concurrent positions

```text
MAX_CONCURRENT_POSITIONS = 1
```

At most one XAUUSD position may exist at any time.

If an order is about to be submitted and an existing position is detected:

```text
REJECT NEW ORDER
```

Do not attempt to reason whether the positions are correlated.

There is only one permitted position.

This also dramatically simplifies follow-up-message correlation.

---

# 4. Trading Sessions

All times are in:

```text
Asia/Kolkata / IST
```

The system must use a timezone-aware clock.

Never use naive local timestamps.

## Morning session

New entries are permitted only:

```text
05:30 IST → 09:00 IST
```

Outside this window:

```text
NO NEW ENTRY
```

Existing positions may continue to be managed.

Session closure must never forcibly close an existing position unless a separate risk policy explicitly requires it.

---

## Evening / New York session

Evening trading begins:

```text
20:00 IST
```

The evening session is limited to:

```text
MAX 1 NEW TRADE
```

The exact evening closing time must be configurable.

Do not assume that "after 8 PM" means the bot can continuously trade all night.

Once the evening trade has been consumed:

```text
NO MORE NEW ENTRIES
```

---

# 5. Daily Trade-State Machine

The bot must maintain a persistent daily state.

Example:

```text
DailyState
├── date
├── morning_trades
├── evening_trades
├── winning_trades
├── losing_trades
├── consecutive_losses
├── successful_70pip_trades
├── morning_disabled
├── evening_trade_consumed
└── daily_kill_switch
```

The state must survive process restarts.

Do not rely exclusively on RAM.

---

# 6. Successful Trade Definition

A successful 70+ pip trade means:

```text
realized winning trade
AND
realized/validated movement >= 70 pips
```

A trade that temporarily reaches +70 pips but subsequently loses does NOT count.

A trade that reaches TP1 but does not satisfy the 70-pip criterion does NOT count.

The exact definition must be implemented deterministically.

---

# 7. Evening Conditional Rule

The bot may take one special evening trade only when the configured daily conditions permit it.

Primary rule:

```text
IF successful_70pip_trades_today >= 2
THEN
    evening trade may be permitted
    MAX SL/RISK = 40 pips
    MAX NEW EVENING TRADES = 1
ELSE
    do not take this conditional evening trade
```

The system must distinguish:

```text
trade eligibility
```

from:

```text
signal validity
```

Passing the daily condition does NOT mean every evening signal should be traded.

The signal must still pass all parser, confidence, price, SL, TP, spread, risk, and execution checks.

---

# 8. Three Consecutive Loss Rule

If:

```text
consecutive_losing_trades >= 3
```

then:

```text
MORNING NEW ENTRIES = DISABLED
```

The bot must wait until the evening session.

The three losses must be consecutive.

A winning trade resets:

```text
consecutive_losses = 0
```

unless another explicit policy says otherwise.

Even after the morning session is disabled:

```text
EVENING = MAX 1 NEW TRADE
```

The bot must NOT interpret the evening session as an unlimited recovery opportunity.

---

# 9. Entry Safety Policy

Entry decisions are asymmetric.

## Entry

If parser confidence is below:

```text
0.95
```

then:

```text
SKIP
```

Reason:

A missed entry costs nothing.

An incorrect entry creates real financial risk.

---

# 10. Signal Parsing

The system must support at minimum:

```text
Text signals
Image signals
Mixed text + image signals
Follow-up messages
Edited/reposted signals
```

Normalize all inputs into one internal structure.

Example:

```python
Signal(
    signal_id,
    source_message_id,
    timestamp,
    symbol,
    direction,
    entry,
    stop_loss,
    take_profit,
    tp1,
    tp2,
    confidence,
    source_type,
    raw_text,
    image_reference,
)
```

---

# 11. Image Parsing

Image parsing must be treated as an unreliable input source.

## Double parsing

Run image extraction twice using independent parsing instructions.

For example:

```text
Parse A
Parse B
```

Both must extract:

```text
direction
entry
SL
TP
TP1
TP2
symbol
```

If critical fields disagree:

```text
REJECT SIGNAL
```

Do not average numbers.

Do not choose the parser that "looks more reasonable."

Do not use a third heuristic to silently resolve disagreement.

Example:

```text
Parser A:
BUY 2650
SL 2640

Parser B:
BUY 2658
SL 2640

RESULT:
REJECT
```

Critical numeric disagreement means uncertainty.

---

# 12. Image Validation

Every parsed image signal must pass deterministic validation.

Example:

```python
def validate_signal(signal, tick):

    require(signal.stop_loss is not None)

    if signal.direction == BUY:
        require(signal.stop_loss < signal.entry)

        if signal.take_profit is not None:
            require(signal.take_profit > signal.entry)

    elif signal.direction == SELL:
        require(signal.stop_loss > signal.entry)

        if signal.take_profit is not None:
            require(signal.take_profit < signal.entry)

    else:
        reject("invalid direction")

    require(
        abs(signal.entry - tick.mid) / tick.mid < 0.005
    )

    sl_distance = pips(
        signal.entry,
        signal.stop_loss
    )

    require(
        MIN_SL_PIPS <= sl_distance <= MAX_SL_PIPS
    )
```

These values must be configurable.

Do not hard-code broker-specific pip assumptions throughout the codebase.

---

# 13. Broker-Aware Price Validation

The system must account for:

```text
bid
ask
spread
symbol digits
contract specification
point size
pip size
minimum stop distance
freeze level
slippage
```

Never assume:

```text
1 pip == 1 point
```

without verifying the broker's XAUUSD specification.

Create one central price utility module.

Example:

```text
PriceUtils
├── pips_to_price()
├── price_to_pips()
├── normalize_price()
├── calculate_spread()
├── calculate_slippage()
├── validate_stop_distance()
└── calculate_protective_stop()
```

---

# 14. Protective Stop Logic

The user's intended protection rule is:

> After TP1, close 50% and move the remaining position's SL beyond the effective cost basis plus 15 pips.

For BUY:

```text
protective_SL =
    entry
    + slippage
    + spread
    + 15 pips
```

For SELL:

```text
protective_SL =
    entry
    - slippage
    - spread
    - 15 pips
```

However, this must be implemented using the broker's actual bid/ask execution mechanics.

Do not blindly apply the same mathematical formula to BUY and SELL without considering:

```text
which side of the spread closes the position
```

The implementation must guarantee that the resulting SL actually locks in the intended minimum protection.

---

# 15. TP Management

## Standard target

When TP1 is reached:

```text
close 50% of the position
```

Then:

```text
move SL to protected level
```

The remaining 50% stays open.

The bot waits for the final TP.

---

# 16. Large Target Rule

A target is considered "large" when:

```text
target distance >= 170 pips
```

For large-target trades:

```text
TP1 → close 50%
TP2 → close 50% of remaining
Final TP → close remaining position
```

Example:

```text
Initial position = 2.00 lots

TP1:
close 1.00
remaining = 1.00

TP2:
close 0.50
remaining = 0.50

Final TP:
close 0.50
remaining = 0
```

Important:

TP2 is NOT another 50% of the original position.

It is:

```text
50% of remaining volume
```

---

# 17. Volume Calculation

Never assume that arbitrary fractional lot sizes are accepted.

The broker may enforce:

```text
minimum volume
maximum volume
volume step
```

Therefore:

```python
def calculate_partial_volume(
    current_volume,
    percentage,
    min_volume,
    volume_step
):
    ...
```

The result must be normalized to broker constraints.

If a requested partial close cannot be executed safely:

```text
DO NOT silently over-close
DO NOT silently increase risk
```

Use a deterministic fallback policy.

---

# 18. Message Correlation

Follow-up messages may refer to existing positions.

Examples:

```text
"move SL to BE"
"close the sell"
"the 2650 buy"
"book half"
"move stop above entry"
```

Correlation must be deterministic whenever possible.

Priority:

```python
def correlate(update, open_trades):

    if update.reply_to_id:
        trade = by_message_id.get(update.reply_to_id)

        if trade:
            return trade, 1.0

    near = [
        t for t in open_trades
        if update.mentions_price_near(
            t.entry,
            tolerance_pips=50
        )
    ]

    if len(near) == 1:
        return near[0], 0.90

    if update.direction:
        matching = [
            t for t in open_trades
            if t.direction == update.direction
        ]

        if len(matching) == 1:
            return matching[0], 0.85

    if len(open_trades) == 1:
        return open_trades[0], 0.80

    return None, 0.0
```

Because:

```text
MAX_CONCURRENT_POSITIONS = 1
```

the single-open-position fallback is safe from multi-position ambiguity.

---

# 19. Ambiguity Policy

Different actions have different risk.

## Entry ambiguity

```text
confidence < 0.95
→ SKIP
```

## Protective ambiguity

Examples:

```text
move to BE
reduce risk
close
partial close
```

Default toward risk reduction.

If exactly one position exists, apply the protective action to that position when the action itself is unambiguous.

## Risk-increasing ambiguity

Examples:

```text
widen SL
add another position
increase lot size
remove SL
move SL farther from price
```

If certainty is insufficient:

```text
IGNORE
```

Never guess.

---

# 20. No Naked Orders

Every new position must be submitted with a valid SL.

The execution layer must reject:

```text
order without SL
invalid SL
SL outside broker limits
SL that violates configured risk
```

If the broker/API supports atomic entry + SL, use it.

If atomic protection is impossible, implement the safest supported mechanism and immediately verify the resulting position.

An order must never be considered successfully protected until the broker confirms the SL.

---

# 21. Daily Loss Kill Switch

The bot must implement a hard daily loss limit.

When the threshold is reached:

```text
DISARM BOT
```

The bot must:

```text
stop accepting new entries
stop processing entry signals
persist kill-switch state
generate an alert
require manual re-arm
```

The kill switch must survive:

```text
restart
crash
reconnection
deployment
```

A process restart must NOT automatically re-enable trading.

---

# 22. Orphan Position Sweeper

Every approximately 60 seconds:

```text
fetch broker positions
compare against internal database
```

If a position exists at the broker but no matching internal record exists:

```text
FLAG ORPHAN
```

The configured safety policy should close the orphan unless there is a deterministic recovery path.

Never leave unknown positions unmanaged.

Every broker position must have:

```text
known origin
known direction
known entry
known SL
known lifecycle
known database record
```

---

# 23. Duplicate Protection

The system must protect against:

```text
duplicate messages
channel reposts
edited messages
network retries
process retries
broker API retries
application restarts
```

Use:

```text
source_message_id
+
content_hash
+
dedupe_window
```

A duplicate must not create another trade.

---

# 24. Order Rate Limiter

Hard limit:

```text
MAX_NEW_ORDER_FREQUENCY = 1 order / 60 seconds
```

This is a runaway guard.

Even if the parser generates 20 signals in one minute:

```text
maximum permitted = 1 order
```

Combined with the one-position rule, this provides an additional safety layer.

---

# 25. Signal Lifecycle

Every signal should pass through:

```text
RAW
 ↓
PARSED
 ↓
NORMALIZED
 ↓
VALIDATED
 ↓
CORRELATED
 ↓
POLICY_CHECKED
 ↓
EXECUTION_APPROVED
 ↓
SUBMITTED
 ↓
BROKER_CONFIRMED
 ↓
MANAGED
 ↓
CLOSED
```

Any stage can produce:

```text
REJECTED
```

The reason must be logged.

---

# 26. Rejection Reasons

Never simply log:

```text
signal rejected
```

Use structured reasons.

Examples:

```text
LOW_CONFIDENCE
IMAGE_PARSE_DISAGREEMENT
INVALID_DIRECTION
INVALID_SL
INVALID_TP
ENTRY_TOO_FAR_FROM_MARKET
SL_TOO_SMALL
SL_TOO_LARGE
SPREAD_TOO_HIGH
DAILY_KILL_SWITCH
MORNING_DISABLED
SESSION_CLOSED
EVENING_TRADE_ALREADY_USED
POSITION_ALREADY_OPEN
THREE_CONSECUTIVE_LOSSES
INSUFFICIENT_70PIP_WINS
DUPLICATE_MESSAGE
AMBIGUOUS_CORRELATION
BROKER_REJECTED
INVALID_VOLUME
```

---

# 27. Logging

Every decision must be explainable after the fact.

Store:

```text
timestamp
source
message_id
content_hash
raw signal
parser result
parser confidence
second parser result
validation result
market price
spread
calculated SL distance
calculated risk
daily state
session
policy decision
correlation result
execution result
broker ticket
position state
```

The system should answer:

> "Why did the bot take this trade?"

and:

> "Why did the bot reject this signal?"

without requiring guesswork.

---

# 28. Archiver — FIRST DEVELOPMENT PRIORITY

Before attempting full automation, build the signal archiver.

The archiver must collect:

```text
historical channel messages
images
timestamps
message IDs
replies
edits
raw text
media references
```

Use Telegram history backfill functionality, such as Telethon's message iteration, to collect historical data.

The objective is to create a real corpus.

Do not develop the parser primarily against invented examples.

---

# 29. Dataset

Create a hand-labeled dataset from archived signals.

Each example should have ground truth:

```text
is_trade_signal
symbol
direction
entry
SL
TP
TP1
TP2
target_size
signal_type
follow_up
correlation_target
```

For images, manually verify the actual displayed numbers.

The dataset must contain difficult examples:

```text
blurry screenshots
multiple prices
multiple SLs
multiple TPs
BUY + SELL text
edited signals
reposted signals
partial close messages
BE messages
screenshots containing unrelated numbers
```

---

# 30. Parser Evaluation

Do not judge the parser by:

```text
"it looks good"
```

Measure:

```text
direction accuracy
entry accuracy
SL accuracy
TP accuracy
TP1 accuracy
TP2 accuracy
signal classification accuracy
false-positive rate
false-negative rate
```

Direction is safety-critical.

---

# 31. Mandatory Live-Gate

Before live autonomous execution:

```text
ZERO direction errors
in 100 consecutive validated signals
```

Additionally:

```text
>= 98% entry/SL field-level accuracy
```

These are minimum gates, not performance targets.

If the parser fails the gate:

```text
DO NOT ENABLE FULL AUTO
```

Continue collecting data and improving the parser.

---

# 32. Shadow Mode

Before execution:

```text
Parser
+
Correlator
+
Policy Engine
+
Risk Engine
```

must run in shadow mode.

The system should produce:

```text
WOULD ENTER
WOULD REJECT
WOULD MODIFY
WOULD CLOSE
```

without actually sending orders.

Compare decisions with expected outcomes.

---

# 33. Demo Stage

After passing parser and policy validation:

```text
DEMO ACCOUNT
```

Run for at least:

```text
2 weeks
```

Monitor:

```text
unexpected entries
wrong direction
wrong SL
wrong TP
duplicate trades
correlation errors
partial-close errors
broker rejection
restart recovery
network failure
orphan positions
daily kill switch
```

Only after this is stable should live execution be considered.

---

# 34. Live Stage

Start with:

```text
minimum practical position size
```

Do not immediately optimize for maximum profit.

The first live objective is:

```text
PROVE EXECUTION CORRECTNESS
```

not:

```text
MAXIMIZE RETURNS
```

---

# 35. Failure Handling

The system must explicitly handle:

```text
Telegram unavailable
broker unavailable
vision parser unavailable
database unavailable
network timeout
duplicate broker response
partial broker execution
process crash
machine restart
clock/timezone errors
invalid market data
spread explosion
market closed
symbol unavailable
order rejected
SL rejected
partial close rejected
```

When uncertain about broker state:

```text
DO NOT submit another order blindly.
```

First reconcile the broker account.

---

# 36. Recovery Procedure

On startup:

```text
1. Connect to database.
2. Connect to broker.
3. Fetch all open positions.
4. Fetch recent orders/deals.
5. Reconcile internal state.
6. Detect orphan positions.
7. Restore daily state.
8. Restore kill-switch state.
9. Verify symbol configuration.
10. Only then enable signal processing.
```

Trading must remain disabled during reconciliation.

---

# 37. Architecture

Recommended components:

```text
                ┌─────────────────────┐
                │ Telegram / Sources  │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │     Archiver        │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Signal Parser       │
                │ Text + Vision       │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Normalizer          │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Validator           │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Correlator          │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Policy Engine       │
                │ Session + Daily     │
                │ Rules               │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Risk Engine         │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Execution Engine    │
                └──────────┬──────────┘
                           │
                           ▼
                ┌─────────────────────┐
                │ Broker / MT5        │
                └─────────────────────┘
```

Separate:

```text
parsing
validation
policy
risk
execution
position management
```

Do not put all logic into one large Python file.

---

# 38. Configuration

Safety-critical values must be centralized.

Example:

```yaml
symbol: XAUUSD

max_concurrent_positions: 1

sessions:
  morning:
    start: "05:30"
    end: "09:00"

  evening:
    start: "20:00"

parser:
  minimum_entry_confidence: 0.95
  require_image_consensus: true

risk:
  max_evening_sl_pips: 40
  large_target_pips: 170
  protective_buffer_pips: 15

execution:
  max_order_frequency_seconds: 60

management:
  tp1_close_percentage: 50
  large_target_tp2_close_percentage_of_remaining: 50

safety:
  orphan_sweep_seconds: 60
  require_sl: true
  manual_rearm_after_daily_kill: true
```

Do not duplicate these values throughout the code.

---

# 39. Testing Requirements

The project must include automated tests for:

## Parser

```text
BUY
SELL
multiple numbers
bad OCR
missing SL
missing TP
ambiguous image
```

## Correlator

```text
reply ID
price match
direction match
one-position fallback
multiple-position ambiguity
no matching position
```

## Risk

```text
40 pip evening limit
normal SL
large SL
daily loss
invalid risk
```

## Session

```text
05:29
05:30
08:59
09:00
19:59
20:00
```

## Position management

```text
TP1
50% close
TP2
50% of remaining
final TP
protective SL
```

## Recovery

```text
restart with open position
orphan position
missing database record
broker/API timeout
duplicate execution request
```

---

# 40. Example Daily Logic

Conceptually:

```python
if daily_kill_switch:
    reject("DAILY_KILL_SWITCH")

elif position_count >= 1:
    reject("POSITION_ALREADY_OPEN")

elif not inside_allowed_session(now):
    reject("SESSION_CLOSED")

elif morning_disabled and is_morning(now):
    reject("MORNING_DISABLED")

elif is_evening(now) and evening_trade_consumed:
    reject("EVENING_TRADE_ALREADY_USED")

elif not signal_is_valid:
    reject("INVALID_SIGNAL")

elif signal.confidence < 0.95:
    reject("LOW_CONFIDENCE")

elif is_evening(now):
    if successful_70pip_trades < 2:
        reject("EVENING_CONDITION_NOT_MET")

    elif signal.sl_distance > 40:
        reject("EVENING_SL_OVER_40_PIPS")

else:
    approve_if_all_other_rules_pass()
```

This is illustrative.

The final implementation must account for all policies and must not accidentally create contradictory conditions.

---

# 41. Important Policy Separation

Do not mix these concepts:

### Signal validity

```text
Is this actually a valid BUY/SELL setup?
```

### Trading eligibility

```text
Is the bot allowed to trade right now?
```

### Risk eligibility

```text
Is the requested trade within risk limits?
```

### Execution validity

```text
Can the broker safely execute it?
```

A signal can be valid but still be rejected because the bot is not allowed to trade.

Example:

```text
Valid BUY signal
+
Morning disabled after 3 consecutive losses
=
REJECT
```

This is expected behavior.

---

# 42. Never Optimize Away Safety

Do NOT remove:

```text
one-position limit
95% entry confidence threshold
image consensus
SL requirement
daily kill switch
orphan sweeper
deduplication
60-second order limiter
manual re-arm
```

simply because they reduce the number of trades.

The system is intentionally conservative.

---

# 43. Development Order

Implement exactly in this general order.

## P0 — Archiver

Build:

```text
Telegram ingestion
history backfill
raw storage
message metadata
image storage
reply relationships
deduplication
```

Do not build full execution yet.

---

## P1 — Dataset + Parser

Create:

```text
hand-labeled corpus
parser
normalizer
validation
parser evaluation
```

Measure real accuracy.

---

## P2 — Correlator

Implement:

```text
reply correlation
price correlation
direction correlation
one-position fallback
ambiguity rejection
```

Test heavily.

---

## P3 — Policy Engine

Implement:

```text
session restrictions
one-position invariant
three-loss rule
two × 70-pip rule
evening 40-pip rule
daily kill switch
```

---

## P4 — Risk Engine

Implement:

```text
SL validation
pip calculations
spread
slippage
position sizing
broker constraints
```

---

## P5 — Shadow Execution

Generate:

```text
WOULD ENTER
WOULD MODIFY
WOULD CLOSE
```

without sending orders.

---

## P6 — Demo

Run:

```text
minimum 2 weeks
```

with complete logging and reconciliation.

---

## P7 — Live

Enable:

```text
minimum position size
```

only after all safety gates pass.

---

# 44. Developer Instructions

When implementing this project:

1. Do not guess broker behavior.
2. Do not guess pip/point conversions.
3. Do not silently recover from ambiguous financial instructions.
4. Do not create naked positions.
5. Do not allow multiple concurrent positions.
6. Do not bypass the policy engine.
7. Do not put risk logic inside the parser.
8. Do not put parser logic inside the execution layer.
9. Do not rely on a human to catch mistakes.
10. Make every safety rule testable.
11. Make every rejection explainable.
12. Persist important state.
13. Reconcile with the broker after restart.
14. Prefer deterministic logic over AI judgment wherever possible.
15. Never increase risk because an AI model "thinks" it is probably correct.

---

# 45. Definition of Done

The system is considered production-ready only when:

```text
[ ] Historical archiver works
[ ] Real signal corpus exists
[ ] Parser evaluated on labeled data
[ ] Zero direction errors in 100 consecutive signals
[ ] >=98% entry/SL field accuracy
[ ] Image double-parse implemented
[ ] Geometric validation implemented
[ ] One-position invariant enforced in execution
[ ] Session engine tested
[ ] Three-loss rule tested
[ ] Two × 70-pip rule tested
[ ] Evening 40-pip restriction tested
[ ] TP1 50% logic tested
[ ] Large-target TP2 logic tested
[ ] Protective SL logic tested
[ ] Broker pip/point conversion verified
[ ] Spread/slippage incorporated
[ ] Daily kill switch tested
[ ] Manual re-arm tested
[ ] Orphan sweeper tested
[ ] Duplicate protection tested
[ ] 60-second order limiter tested
[ ] Restart recovery tested
[ ] Broker reconciliation tested
[ ] Shadow mode completed
[ ] Demo test completed
[ ] Minimum-size live test completed
```

---

# 46. Master Principle

The final system should not try to be the smartest trader.

It should try to be the hardest system to accidentally kill.

The architecture should therefore favor:

```text
determinism > cleverness
validation > prediction
rejection > guessing
risk reduction > risk increase
one position > correlated positions
explainability > black-box decisions
capital preservation > trade frequency
```

The bot should only trade when:

```text
SIGNAL IS VALID
AND
PARSER IS CONFIDENT
AND
IMAGE CONSENSUS EXISTS
AND
MARKET CONDITIONS ARE VALID
AND
SESSION ALLOWS TRADING
AND
DAILY STATE ALLOWS TRADING
AND
RISK LIMITS ARE SATISFIED
AND
NO POSITION IS CURRENTLY OPEN
AND
BROKER CAN SAFELY EXECUTE
AND
SL IS ATTACHED
```

Otherwise:

```text
NO TRADE.
```

That is not a failure.

That is the intended behavior.
