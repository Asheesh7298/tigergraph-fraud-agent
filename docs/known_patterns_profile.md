---
doc_id: known-patterns-profile
title: The five documented patterns, with their measured shape
kind: typology
source: README.md pattern section, quantified from closed_cases_history.csv
patterns: [card_testing, card_not_present_fraud, card_not_present_new_device, out_of_region_use, account_takeover]
rules: [R1, R2, R3, R4, R5]
---

# The five documented patterns, with their measured shape

The README describes the patterns qualitatively. These are the same patterns
measured against the 4,665 confirmed-fraud cases, so an assessment can be
checked against what each one actually looks like rather than against the
adjective.

| Pattern | n | Share | Median exposure | Median txns | 90th pct | Max |
|---|---|---|---|---|---|---|
| `card_not_present_fraud` | 1,404 | 30.1% | $100 | 1 | 3 | 13 |
| `account_takeover` | 1,205 | 25.8% | $252 | 2 | 7 | 211 |
| `card_not_present_new_device` | 1,076 | 23.1% | $179 | 2 | 8 | 356 |
| `out_of_region_use` | 955 | 20.5% | $235 | 1 | 5 | 32 |
| `card_testing` | 16 | **0.34%** | $847 | **21** | — | 71 |
| `undocumented` | 9 | 0.19% | $1,871 | 4 | — | 4 |

## What the numbers change

**Episodes are small.** The median confirmed fraud episode is one or two
transactions. Four of the five patterns have a median of 1–2. An assessment
that routinely returns eight or ten affected transactions is over-collecting.

**But do not cap the episode.** Across the history there are 14,055
fraudulent transactions; capping every episode at four would capture only
8,881 of them — **36.8% of fraudulent transactions would be missed.** The
long tail is real: `account_takeover` reaches 211 transactions and
`card_not_present_new_device` reaches 356. Use a stopping condition — extend
while the next transaction shares a linking feature (same device profile,
same unseen region, same anomalous product, or inside the burst window) and
stop at the first that shares none — rather than a fixed limit.

**Card testing is rare and long.** It is 0.34% of confirmed fraud, and when
it does occur the median episode is **21 transactions**, not the four in the
README's worked example. Because that worked example is the one complete
answer published, it is the easiest pattern to over-predict. A three-legged
sequence of small authorisations is not automatically card testing, and
labelling several of the twenty cases `card_testing` almost certainly means
the example was pattern-matched rather than the data.

## Pattern classification order

Rings must be tested **before** card testing, or a four-leg structuring burst
gets filed as a testing run:

1. Full four-part anonymised-proxy device profile shared across ≥3 cards in
   the window → `undocumented` (see ring-a-shared-device)
2. Exactly 4 online transactions ≤45 min, each $450–499 → `undocumented`
   (see ring-b-threshold-structuring)
3. ≥3 online authorisations under $5 within an hour, then a larger purchase
   → `card_testing` (R5)
4. Card-present use in an unseen region while home activity continues in
   parallel → `out_of_region_use` (R2, R3)
5. Both channels inside one episode with match-flag anomalies →
   `account_takeover`
6. Online only, device marked `New` for the account →
   `card_not_present_new_device`
7. Online only, amounts or products off the cardholder's baseline →
   `card_not_present_fraud`
8. Consistent with baseline → `none`

## A caution on the unnamed columns

`M1`–`M9` are documented as match flags and are legitimate evidence for
`account_takeover`. But they are populated almost exclusively on
`ProductCD = W` (in-person) rows; on `C`, `R` and `H` they are structurally
empty. **Their absence on an online transaction is not a signal** and must
not be cited as one.

`C1`–`C14`, `D1`–`D15` and `V1`–`V339` are unnamed engineered features. They
may be used as signals, but the README asks us to be honest about what a
column is, and "V127 was elevated" is not something a regulator or an analyst
can act on. They are not loaded into the graph for that reason.
