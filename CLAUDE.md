# CLAUDE.md — TigerGraph Agentic Fraud Investigation

Full working context for this project. Read this before touching any file.
Everything here was derived from the dataset or decided deliberately; the
reasoning is included so you don't re-open settled questions or repeat
mistakes that were already caught.

---

## 1. The task

**HH Goa 2026 — TigerGraph partner challenge.** Build an agentic fraud
investigation system on the IEEE-CIS (Vesta) dataset. Take a fraud alert,
investigate it using a knowledge graph and prior cases, decide what kind of
fraud it is (if any), how far it goes, and what the bank should do next —
including knowing when more evidence is needed before deciding.

**Deadline: 24 September 2026.** Planning finished 19 September. **Five working days.**

**Developer:** Asheesh Kumar — IT undergrad, Galgotias College. Stack is
Python / Django / PostgreSQL / FastAPI. Has not used LangGraph (this matters,
see §7).

### Mandatory deliverables — all six or the submission is incomplete

- [ ] Working agent, runnable end to end
- [ ] GitHub repo with README
- [ ] `cases/HHG-001.json` … `HHG-020.json` — twenty answer files
- [ ] 3–5 minute demo video
- [ ] Technical blog post
- [ ] Social post on X or LinkedIn tagging **@TigerGraphDB**

### Required components — no substitutions allowed

1. TigerGraph Savanna or Community Edition
2. GSQL + graph algorithms
3. TigerGraph MCP
4. GraphRAG
5. A user interface

### Judging weights

| Criterion | Weight |
|---|---|
| Investigation accuracy | 25% |
| Next best action | 25% |
| Case summary & explainability | 10% |
| Agentic design & engineering | 15% |
| Innovation | 15% |
| Demo quality | 10% |

**60% is read directly out of the twenty JSON files.** Scored against an
answer key we cannot see.

---

## 2. Dataset facts

| File | Contents |
|---|---|
| `transactions.csv` | 590,742 rows, 393 Vesta columns + 4 added. ~708 MB. **No fraud flag.** |
| `identity.csv` | 144,432 rows, 41 columns. Online transactions only. Joins on `TransactionID`. |
| `closed_cases_history.csv` | 5,565 finished investigations, Jul–Oct. 4,665 confirmed fraud, 900 cleared. |
| `case_pack.csv` | The 20 exam cases, Nov–Dec. |

Added columns: `customer_id`, `ts` (real timestamp), `channel`
(`in_person` / `online`), `risk_score` (0–1).

`TransactionID`, `card1`, `TransactionDT`, `TransactionAmt` were disguised so
answers can't be recovered from the public Kaggle file. **Using the original
IEEE-CIS files to recover outcomes is disqualification.**

Case pack triggers: **11 risk_score · 8 customer_report · 1 analyst_request.**

---

## 3. Verified findings — the actual edge

Every number below was computed from the CSVs. Re-derivable.

### 3.1 The SAR rule fits history exactly

```python
file_report = (exposure_usd > 1000) or bool(connected_card_ids)
```

**4,665 / 4,665 confirmed-fraud cases. Zero false positives, zero false
negatives.** No cleared case ever filed a report.

Breakdown: 393 cases exceed $1,000 + 4 Ring-A cases under $1,000 but with
connected cards = 397 filings. Exact.

**Important caveat.** These cases are machine-generated, so this recovers the
*generator's* rule. The policy adds disjuncts (shared region cluster, other
customer's fraud, undocumented pattern) that never fired historically because
no such cases existed. **Ship the policy's full condition** — implemented in
`policy.should_file_sar()`. Under-filing on the exam is the risk.

### 3.2 Two undocumented rings, not one

The nine `undocumented` closed cases split cleanly.

**Ring A — shared device** (CC-2649, CC-2971, CC-2985, CC-3035)

```
DeviceInfo ⊃ SM-G935F
id_30 = Android 7.0
id_31 = chrome … for android
id_23 = IP_PROXY:ANONYMOUS
id_15 = New
```

23 connected cards. Exposures $108–$390 — all under $1,000, all filed reports.
These are the **only four fraud cases in the entire history** with non-empty
`connected_card_ids`.

In `identity.csv`: 563 records contain `SM-G935F`, only **114** also carry
`IP_PROXY:ANONYMOUS`. Tight fingerprint.

**Ring B — threshold structuring** (CC-3748, CC-3841, CC-3907, CC-4086, CC-4124)

```
exactly 4 online transactions
within ~40 minutes
each ≈ $468–481  (deliberately under a $500 authorisation ceiling)
total ≈ $1,871–1,923
no connected cards
```

**HHG-014 is the analyst-request case:** *"several cards this month show
purchases from the same unusual device profile."* That is Ring A described in
plain language. Expect Ring B somewhere in the pack too.

### 3.3 Cleared cases have exactly three signatures

All 900:

| Count | Reason |
|---|---|
| 716 | Cardholder confirmed **travel** to the billing region |
| 158 | Cardholder confirmed purchase from a **new phone** |
| 26 | Unusual amount, cardholder confirmed intent |

**Out-of-region alone and new-device alone are usually legitimate.** This is
the empirical basis for policy R1 and for the README's warning that "an agent
that blocks everything scores badly."

### 3.4 Historical action sets — three shapes only

| Shape | Count |
|---|---|
| `CREATE_CASE \| BLOCK_CARD` | 4,268 |
| `CREATE_CASE \| BLOCK_CARD \| FILE_REPORT` | 397 |
| `VERIFY_WITH_CUSTOMER \| CLOSE_NO_FRAUD` | 900 |

These are *final* sets recorded after customer contact. Exam wants
`initial` **and** `final`, so initial sets are softer — typically
`VERIFY_WITH_CUSTOMER` under R1 — converging on these after the assumed response.

### 3.5 Per-pattern profile

| Pattern | n | Median exp | p50 txns | p75 | p90 | p99 | max | ≥10 txns |
|---|---|---|---|---|---|---|---|---|
| `card_not_present_fraud` | 1,404 | $100 | 1 | 2 | 3 | 8 | 13 | 0.6% |
| `account_takeover` | 1,205 | $252 | 2 | 3 | 7 | 32 | **211** | 8.0% |
| `card_not_present_new_device` | 1,076 | $179 | 2 | 4 | 8 | 30 | **356** | 8.1% |
| `out_of_region_use` | 955 | $235 | 1 | 3 | 5 | 19 | 32 | 4.3% |
| `card_testing` | 16 | $847 | **21** | — | — | — | 71 | — |
| `undocumented` | 9 | $1,871 | 4 | — | — | — | 4 | — |

### 3.6 Episode sizing — a fixed cap loses 37% of recall

Total fraud transactions in history: **14,055**.
Capping every episode at 4 captures **8,881 (63.2%)**.
**A hard cap of 4 loses 36.8% of fraudulent transactions.**

**Do not use a fixed cap.** Use a stopping condition — see §6.2.

### 3.7 Card testing is rare and long

16 of 4,665 confirmed-fraud cases = **0.34%**. Median episode **21 transactions**,
not the four shown in the README's worked example.

If the agent labels three exam cases `card_testing`, it pattern-matched the
example rather than the data. At most one, likely zero.

### 3.8 The README example uses fictional IDs

`T0412877`, card `C00377-K1`, device `D000731` — **none exist in this dataset.**
Real transaction IDs are bare integers like `3514030`.

Copy the example's *shape*. Never its values. Made-up IDs score zero.

---

## 4. The R7 trap — highest-leverage finding

**Zero of 900 cleared cases involve a customer dispute.** All 900 are model
false alarms. The history contains **no example whatsoever** of a customer
disputing a charge that turned out legitimate.

Yet policy R7 exists:

> *"When the customer disputes a charge that matches their own recurring
> pattern (same merchant, same amount, monthly), recommend `CREATE_CASE`,
> `VERIFY_WITH_CUSTOMER`, and `WARN_CUSTOMER`. **Do not block.**"*

Rules get written for situations that occur. R7 has zero historical instances,
which means it is almost certainly **in the exam**. The amounts fit —
HHG-003 $49.00, HHG-009 $30.02, HHG-008 $55.68, HHG-018 $39.08 are
subscription-shaped.

An agent that treats `customer_report` as a fraud prior will block those cards
and take a **double hit**: wrong verdict *and* a policy breach on
next-best-action. That's 50% of the grade.

**Implemented as a hard gate** in `policy.recurrence_check()` and the first
branch of `policy.decide()`. It pre-empts R2 deliberately.

---

## 5. Architecture

```
TigerGraph Savanna
  GRAPH    Customer ─OWNS→ Card ─MADE→ Transaction
           Transaction ─FROM_DEVICE→ DeviceProfile
           Transaction ─BILLED_IN→ BillingRegion
           Transaction ─PURCHASER_EMAIL→ EmailDomain
           Transaction ─NEXT→ Transaction
           ClosedCase ─INVOLVES→ Transaction
           Case ─SIMILAR_TO→ ClosedCase        (agent-written memory link)
  VECTORS  5,565 closed-case narratives · fraud policy · 5 patterns
           · Ring A/B write-ups · 1 FinCEN SAR narrative doc
        │  TigerGraph MCP
        ▼
AGENT  (plain Python state machine, dataclass state)
  TRIGGER → INVESTIGATE → RETRIEVE → ASSESS
     → [snapshot initial] → REQUEST EVIDENCE → RE-ASSESS
     → POLICY ENGINE → EXPLAIN → PERSIST
        │
        ▼
cases/HHG-0NN.json  +  Streamlit read-only viewer
```

### The split that decides the score

| Decided by LLM | Decided by code |
|---|---|
| `verdict` | `exposure_usd` |
| `pattern` | `sar.file` |
| `fraud_probability` | every `route` |
| episode membership | action sets (R1–R10) |
| `evidence[]` prose | stop condition |
| `summary`, `sar.narrative` | status derivation |

Anything in the right column that an LLM writes instead is a self-inflicted
loss. Most teams will hand all of it to the model and get routes wrong and
SAR flags inconsistent with their own action lists.

### Six GSQL queries, exposed via MCP

| Query | Returns |
|---|---|
| `card_window(card_id, ts, hours)` | Ordered transactions in a window |
| `customer_baseline(customer_id, before_ts)` | p50/p90 amounts, usual products, regions, devices, channel mix |
| `device_neighbors(profile_key, from, to)` | Every card/customer on this device profile |
| `region_history(card_id, addr1)` | Has this card ever billed here |
| `similar_closed_cases(text, k)` | Vector search → case IDs, outcome, pattern |
| `ring_detect(from, to, min_cards)` | **Connected components** over shared device profiles |

`ring_detect` is the innovation play. Implement as a real traversal over
`Transaction → FROM_DEVICE → DeviceProfile ← FROM_DEVICE ← Transaction`,
restricted to Nov–Dec. Ring A should surface as a component spanning 20+ cards.
**Do not hardcode the SM-G935F string** — finding it algorithmically is the
difference between using TigerGraph and *using* TigerGraph.

`customer_baseline` is the accuracy driver. Nearly every verdict turns on
"is this consistent with how this cardholder behaves."

---

## 6. Key implementation rules

### 6.1 Load narrow, not short

**397 columns → 58.** 708 MB → ~103 MB. 6.8× reduction, no investigative
signal lost.

```
Keep:  TransactionID, TransactionDT, TransactionAmt, ProductCD,
       card1–card6, addr1, addr2, dist1, dist2,
       P_emaildomain, R_emaildomain,
       C1–C14   (entity counts)
       D1–D15   (day deltas)
       M1–M9    (match flags — needed for account_takeover)
       customer_id, ts, channel, risk_score
Drop:  V1–V339  (unnamed engineered features, cannot be honestly cited)
```

Do **not** load fewer rows — that breaks ring detection and baselines.

This single decision is what makes five days feasible. Implemented in
`prep_data.py`.

### 6.2 Episode expansion by stopping condition

```
start at the flagged transaction
walk backward and forward along Card ─MADE→ Transaction ─NEXT→
include next while it shares a linking feature:
    same device profile
    OR same unseen billing region
    OR same anomalous product code
    OR inside the burst window (≤2h)
stop at the first transaction sharing none
```

Walk **backward** too, to populate `first_suspicious_txn_id`. The README says
the flagged transaction "is not necessarily where the fraud started, and it
may not be fraud at all." The agent must be able to conclude *this transaction
is fine but these three others are not.*

### 6.3 Pattern classifier — first match wins

Rings must sit **above** card testing or Ring B gets misfiled as a testing run.

```
1. SM-G935F + Android 7.0 + chrome-android + ANONYMOUS proxy,
   shared across ≥3 cards in window              → undocumented (Ring A)
2. exactly 4 online txns ≤45min, each $450–499   → undocumented (Ring B)
3. ≥3 online auths <$5 in 1h then larger buy     → card_testing
4. card-present in unseen region, home activity
   continues in parallel                          → out_of_region_use
5. both channels in episode, M-flag anomalies     → account_takeover
6. online only, id_15 == 'New'                    → card_not_present_new_device
7. online only, off-baseline amounts/products     → card_not_present_fraud
8. consistent with baseline                       → none
```

### 6.4 Probability anchors — prior, tune against holdout

| Evidence state | Band |
|---|---|
| Risk score alone, baseline consistent | 0.10–0.25 |
| One anomaly, nothing else | 0.30–0.50 |
| Two independent anomalies | 0.55–0.70 |
| Customer denial | 0.75–0.85 |
| Denial + ring membership | 0.85–0.95 |

Put this table in the prompt. Un-anchored models cluster everything at 0.8.
These numbers are **heuristics, not derived** — fit them against the holdout.

### 6.5 Distribution check before submit

Expected shape, from "half the cases are legitimate" plus the 11/8/1 split:

- ~8–11 fraud · ~7–10 legitimate · ~1–3 uncertain
- at most one `card_testing`, likely zero
- one or two `undocumented` (HHG-014 ≈ Ring A)
- SAR on ~2–5 of twenty

**Seventeen fraud verdicts is a bug, not a breakthrough.**
Implemented in `bootstrap.validate_all()`.

### 6.6 Invariants — enforced in `schema.py`, cannot be bypassed

```
sar.file == ("FILE_REPORT" in final_actions)          ← README calls this out
exposure == sum(abs(amt) for t in affected_txn_ids)
verdict=="legitimate" → affected==[] and exposure==0 and sar.file False
pattern=="undocumented" → pattern_description != ""
sar.file False → narrative=="" and subjects==[] and total==0
affected_txn_ids non-empty → first_suspicious_txn_id non-empty
written_to_graph True → graph_case_id non-empty
every action reason cites R1..R10                      ← policy §7
initial == final → what_changed == "nothing" (and vice versa)
every ID exists in the dataset                         ← made-up IDs score zero
```

### 6.7 Chronological case memory — innovation play

Sort the pack by `opened_at` and process in order:

```
HHG-017 (Nov 12) → HHG-015 (Nov 17) → HHG-006 (Nov 22)
→ HHG-014 (Nov 22, THE RING) → HHG-002 → HHG-018 → HHG-019
→ HHG-010 → HHG-020 → HHG-001 → HHG-007 → HHG-005 → HHG-013
→ HHG-003 → HHG-016 → HHG-012 → HHG-008 → HHG-009
→ HHG-011 → HHG-004 (Dec 29)
```

HHG-014 lands **fourth**. The sixteen cases after it can retrieve it from the
graph and cite it in `similar_prior_cases`. Real demonstrable memory across
sixteen output files. Cost: a `sorted()` call. Most teams will run the twenty
in parallel and have nothing to show for the memory criterion.

---

## 7. Decisions already made — do not re-open

| Decision | Reason |
|---|---|
| **Plain Python state machine, NOT LangGraph** | Rubric explicitly permits custom implementations. Asheesh hasn't used LangGraph. A dataclass + one function per stage gives identical explicit transitions with zero learning time. |
| **Narrow the load, don't shorten it** | Fewer rows breaks ring detection and baselines. Fewer columns costs nothing. |
| **Policy engine is pure code, no LLM** | ~half the scored fields are deterministic. |
| **Ship policy's full SAR condition, not the 2-term historical fit** | Historical fit is the generator's rule; policy adds disjuncts that never fired. |
| **Holdout shrunk 60 → 30 cases** | Five days. Still the only real feedback signal. |
| **Streamlit read-only viewer, not a real dashboard** | UI is 10%; viewer is ~3 hours. |
| **One FinCEN doc, not eight** | Only SAR Narrative Guidance materially affects output. |
| **Optional monitoring folder: CUT** | Innovation points, but it's a whole second pipeline. |
| **V1–V339: dropped entirely** | 339 unnamed features, 6.8× load width, cannot be honestly cited as evidence. |

---

## 8. Current code state

Four files exist, all tested.

### `prep_data.py`
Narrows `transactions.csv` 397 → 58 columns in chunks. Then builds the
`card_id` mapping and **self-checks it against the 20 exam card IDs**.

`card_id` is not a column in the source — the pack uses `C12382-K1` but it has
to be constructed from `(customer_id, card fingerprint)` ordered by first
appearance. **If the self-check prints anything other than 20/20, stop and fix
it before loading the graph.** Every answer file depends on those IDs.

```bash
python prep_data.py --in data/transactions.csv --out data/txn_narrow.csv
```

### `schema.py`
Pydantic models for the full answer format. Every invariant from §6.6 enforced
at construction. **An invalid answer cannot be built.** Also contains
`IdRegistry` for validating every ID against the real dataset.

### `policy.py`
The deterministic engine. `ROUTES`, `route_for()`, `should_file_sar()`,
`recurrence_check()` (R7), `decide()` implementing R1–R10, `status_for()`.

Verified against the README's worked example — produces
`BLOCK_CARD(L1)`, `CREATE_CASE(auto)`, `FILE_REPORT(L2)`,
`MONITOR_CONNECTED_CARDS(auto)`, matching the README's `final` list.

Route boundary verified: exposure $2,500.00 → L1, $2,500.01 → L2.

### `bootstrap.py`
Emits twenty schema-valid placeholder files from `case_pack.csv` alone — no
graph, no LLM. Also contains `validate_all()` with the distribution check.

```bash
python bootstrap.py --case-pack data/case_pack.csv --out cases/
# -> 20 files written. All schema-valid, all invariants satisfied. 0 problems.
```

### ⚠ KNOWN DEFECT — fix this first

In `policy.decide()`, the R5 card-testing branch fires regardless of
`customer_response`. When denial has already settled the case,
`DECLINE_TRANSACTION` and `STEP_UP_AUTH` should **not** persist into `final` —
blocking the card subsumes declining the authorization. In the README's
example those two appear in `initial` only.

**Fix:** gate the R5 branch on `customer_response is None`. Roughly four lines.

---

## 9. The five days

| Day | Work | Done means |
|---|---|---|
| **1 · Fri 19** | Savanna up · narrow + load · schema · stub writer · policy engine | **20 valid files exist.** Policy engine unit-tested. |
| **2 · Sat 20** | Six GSQL queries · MCP wired · **investigate HHG-014 by hand** | One tool proven end to end. `HHG-014.json` hand-written as the template. |
| **3 · Sun 21** | Vector store · agent state machine · first full run · **draft blog + video script** | Real verdicts on all 20. |
| **4 · Mon 22** | Holdout (30 Oct cases) · score · fix · re-run · Streamlit viewer | Accuracy measured, not guessed. |
| **5 · Tue 23** | Hand-review all 20 · demo video · blog · social · repo tidy | All six deliverables done. |
| **Wed 24** | **Submit early in the day.** Do not leave it to the deadline hour. | |

**Day 1 is the one that matters.** The stub writer and policy engine don't
touch the graph — build them while the load runs. By tonight there should be a
submission-shaped repo on disk.

**Day 3's blog draft is not optional.** Five-day submissions die on the
writing, not the code. A missing blog post costs more than a mediocre agent.

---

## 10. Holdout harness — next thing to build

Not yet written. Doesn't need the graph, so it can be built in parallel.

```
Take ~30 closed cases from October (last history month).
Strip: outcome, pattern, txn_ids, exposure, report_filed, connected_card_ids.
Reshape into case_pack.csv-shaped rows.
Run the agent. Score against withheld truth.

Metrics: verdict accuracy · pattern accuracy
         affected_txn_ids precision/recall
         SAR precision/recall · calibration error (|predicted − outcome|)
```

**Caveat to state in the repo:** October cases are generated with templated
narratives, so holdout accuracy reads optimistically. It still catches
direction-of-error, which is the point. The R5 defect in §8 is exactly the
class of bug it would have caught.

---

## 11. Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| Load eats day 2 | **Fatal** | Narrow to 58 cols first. Verify counts. |
| Blog/video not done | **Fatal** | Draft day 3, finish day 5. Treat as deliverables. |
| Made-up IDs in output | **Fatal** | `IdRegistry.check()` before every write. |
| MCP is rough | High | Tools are plain Python over GSQL; MCP is a thin wrapper. Swap transport, don't rewrite. |
| Savanna auto-stops mid-run | High | Auto-start on. Per-case checkpointing. Resumable runner. |
| Over-blocking customer reports | High | R7 gate + distribution check. |
| Episode over/under-collection | High | Stopping condition, not a fixed cap. |
| Card testing over-predicted | Medium | Classifier order + distribution warning. |

---

## 12. The honest framing

With five days you are not going to out-engineer a team that had three weeks.

**The win condition is completeness plus correctness on deterministic fields.**
A large share of teams will arrive on the 24th with a half-finished agent, no
blog post, and twelve of twenty files. Shipping all six deliverables, with
correct routes and a SAR flag consistent with the action list, puts you ahead
of most of the field. The ring finding and the chronological memory are the
differentiators on top of that.

**Priority order when time runs short:**

1. Twenty valid files with correct deterministic fields
2. All six deliverables existing
3. Ring detection working
4. Everything else
