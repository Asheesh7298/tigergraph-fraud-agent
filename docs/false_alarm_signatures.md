---
doc_id: false-alarm-signatures
title: What a cleared case looks like — calibration against 900 false alarms
kind: calibration
source: derived from closed_cases_history.csv
patterns: [none]
rules: [R1, R3, R7]
---

# What a cleared case looks like

Half the case pack is legitimate. The README says so, and an agent that
blocks everything scores badly. This document is the empirical basis for
holding back.

## The three signatures, and nothing else

All 900 cleared investigations in the closed history fall into exactly three
groups:

| Count | Reason the alert was a false alarm |
|---|---|
| **716** | Cardholder confirmed **travel** to the billing region |
| **158** | Cardholder confirmed a purchase from a **new phone** |
| **26** | Unusual amount, cardholder confirmed they intended it |

Every cleared case has `pattern = none` and `report_filed = No`. No cleared
case ever filed a report.

## What this means for weak signals

**Out-of-region use, on its own, is usually legitimate.** It is the single
most common false alarm in the entire history — 716 of 900. A billing region
the card has not used before is a reason to verify, never a reason to block.
What separates a cloned card from a trip is whether the cardholder's normal
home activity *continued in parallel*; `region_history` returns exactly that.

**A new device, on its own, is usually legitimate.** 158 of 900. People buy
phones. `id_15 = New` strengthens a case that already has something else in
it; alone it is weak. Note that `card_not_present_new_device` is nonetheless
a real confirmed pattern with 1,076 instances — the difference is always the
corroborating evidence, not the device flag.

**An unusual amount, on its own, is usually legitimate.** 26 of 900, and only
the weakest of the three. But it is still a single signal, and policy R1
governs: below probability 0.70 on one signal, verify before any block.

## The trap in the customer reports

**Zero of the 900 cleared cases involve a customer dispute.** Every one is a
model false alarm. The history therefore contains *no example whatsoever* of
a customer disputing a charge that turned out to be legitimate.

An agent that learns its priors from this history will conclude that a
customer report means fraud. Policy **R7** exists precisely for the case the
history does not contain: a cardholder disputing a charge that matches their
own recurring pattern. Blocking those cards is a double loss — wrong verdict
and a policy breach on the recommended action.

The recurrence test must come from the graph, not from precedent. Ask
`customer_baseline` how many times this exact amount and product have already
appeared on this card. A charge the cardholder makes constantly is not
fraud they failed to recognise; it is a charge they have forgotten they
authorise.

Note that the policy words R7 as "same merchant, same amount, monthly". No
case in this dataset shows a clean 28–31 day cadence. The legitimate disputes
here are instead charges repeated *far more often* than monthly — dozens of
occurrences days apart. Read R7 as **established recurring cadence** and say
so in the reason, so the widened reading is visible as a decision rather than
an oversight.

## Calibration for the twenty

Expected shape of a well-calibrated submission:

- roughly 8–11 fraud, 7–10 legitimate, 1–3 uncertain
- **at most one `card_testing`, probably zero** — it is 0.34% of confirmed
  history (16 of 4,665), and the README's worked example is card testing,
  which makes it the easiest pattern to over-predict
- two `undocumented` (the two rings)
- a report on roughly 2–5 of the twenty

Seventeen fraud verdicts is a bug, not a breakthrough.
