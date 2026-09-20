---
doc_id: ring-b-threshold-structuring
title: Undocumented pattern B — threshold structuring under an authorisation ceiling
kind: typology
source: derived from closed_cases_history.csv and transactions.csv
patterns: [undocumented]
rules: [R9]
---

# Undocumented pattern B: threshold structuring under an authorisation ceiling

A single card drained in one sitting through four online purchases, each
deliberately sized just under a $500 authorisation ceiling.

## Fingerprint

| Property | Value |
|---|---|
| Transactions | exactly **4**, online |
| Window | 30–45 minutes |
| Each leg | **$450–$499** |
| Total | **$1,870–$1,925** |
| Product code | uniform across the burst (`C` in observed cases) |
| Connected cards | **none** |

The tell is not any single transaction — $478 is unremarkable — but the
**repetition just below a round ceiling**. Four legs at $456–$488 in half an
hour is a deliberate shape, not a shopping pattern. Splitting a ~$1,900
purchase into four sub-$500 authorisations is how an actor stays under a
per-transaction approval limit.

## Distinguishing it from Ring A

Ring B leaves **no shared-device trail**. In the observed case-pack instance
the four legs alternate between two device profiles (`Trident/7.0` desktop
and `iOS Device` mobile), both of which are among the most common strings in
the dataset — 7,446 and 19,805 records respectively. They are not a traceable
fingerprint. Device-switching mid-burst on one card is itself worth citing as
anomalous, but it will not lead to other cards.

Consequently `connected_card_ids` is empty and the report is filed on the
**amount** limb of policy 3a, not the shared-origin limb.

## Distinguishing it from card testing

Card testing is small authorisations *then* a large purchase, to check a
number works. Ring B legs are all the same size and all large. Card testing
is also rare — 16 of 4,665 confirmed cases, 0.34%, with a median episode of
21 transactions. A four-transaction burst of near-identical large amounts is
not a testing run.

## Historical instances

Five closed cases, all confirmed fraud, all recorded as `undocumented`:
**CC-3748, CC-3841, CC-3907, CC-4086, CC-4124**. Exposures $1,871.13,
$1,899.30, $1,905.21 (approx.), $1,922.37 and $1,922.65. Every one filed a
report — exposure exceeds $1,000 in all cases.

## Policy handling

- **R9 (undocumented pattern)** applies: open a case, file a report, escalate
  to an analyst, and describe the structuring in `pattern_description`.
- Exposure is the **sum of all four legs**, not the flagged one. Reporting
  only the flagged leg understates it by roughly 75% and will also break the
  $1,000 threshold test.
- The episode must be recovered by walking the card's transactions backwards
  as well as forwards from the flagged one — the flagged leg is often the
  last, not the first.
- `first_suspicious_txn_id` is the earliest leg of the burst.

## How to find it

`card_window(card_id, ts - 3h, ts + 3h)` around the flagged transaction's own
timestamp. Resolve that timestamp from the graph: a case may be opened hours
after the activity, and windowing off `opened_at` can miss the burst
entirely.
