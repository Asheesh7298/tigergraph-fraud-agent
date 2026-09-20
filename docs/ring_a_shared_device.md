---
doc_id: ring-a-shared-device
title: Undocumented pattern A — shared anonymised-proxy device ring
kind: typology
source: derived from closed_cases_history.csv and identity.csv
patterns: [undocumented]
rules: [R6, R9]
---

# Undocumented pattern A: shared anonymised-proxy device ring

A single mobile device profile, hidden behind an anonymising proxy, used to
make small online purchases on dozens of unrelated cards over a few weeks.

## Fingerprint

Membership requires **all four** identity attributes to match, not just the
device model:

| Attribute | Value |
|---|---|
| `DeviceInfo` | `SM-G935F Build/NRD90M` |
| `id_30` (OS) | `Android 7.0` |
| `id_31` (browser) | `chrome 62.0 for android` |
| `id_23` (proxy) | `IP_PROXY:ANONYMOUS` |
| `id_15` | `New` on every account |

**The model alone is not the pattern.** The Samsung Galaxy S7 was one of the
most common handsets of 2016: 563 identity records in this dataset contain
`SM-G935F`, but only 114 carry the anonymising proxy as well. Matching on
`DeviceInfo` substring pulls in unrelated cardholders — in the case pack it
falsely implicates the cards behind HHG-008 and HHG-011, both of which have
`id_23` and `id_30` empty. Require the full four-part profile.

## Behaviour

- Purchases are `ProductCD = C`, online, between roughly $36 and $256.
- One to two transactions per day, continuously, across many cards at once.
- Individual cards see only two or three transactions, days apart — small
  enough that no single card looks alarming.
- **Risk scores are low.** The bank's model scored the case-pack legs of this
  ring at 0.05, 0.10 and 0.22. No risk-score trigger would ever raise it. The
  ring is only visible as a shared-device cluster in the graph, which is why
  it reached an analyst instead.
- Cardholders' own histories are usually ordinary in-person spend, so the
  online activity is a clean break from baseline.

## Historical instances

Four closed cases were confirmed as fraud and recorded as `undocumented`:
**CC-2649, CC-2971, CC-2985, CC-3035**. Exposures were $108.36, $140.95,
$381.40 and $390.04 — all under the $1,000 reporting threshold, yet **all
four filed a suspicious activity report**, because the shared device connects
them to other cards. They are the only confirmed cases in the entire history
with a non-empty `connected_card_ids`.

## Policy handling

- **R6 (shared origin)** applies: name the shared device profile, open a case,
  file a report, and place every card sharing the profile under monitoring.
- **R9 (undocumented pattern)** also applies: it matches none of the five
  documented patterns, so describe it in `pattern_description` and escalate to
  an analyst rather than forcing it into `card_not_present_new_device`.
- The report is filed on the **shared-origin** limb of policy 3a, not the
  amount limb. Exposure will usually be a few hundred dollars.
- `connected_card_ids` should list the other cards on the profile in the
  window; `connected_device_profiles` should carry the full profile string.

## How to find it

`ring_detect(from, to, min_cards)` aggregates distinct cards per device
profile across the window and returns the clusters above the threshold,
ranked. The ring surfaces as the largest cluster without the fingerprint
being supplied — see `queries/ring_detect.gsql`. Confirm membership with
`device_neighbors(profile_key, from, to)`.
