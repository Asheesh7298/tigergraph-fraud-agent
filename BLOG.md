# Finding fraud rings a risk model can't see: an agentic investigator on TigerGraph

*Built for the TigerGraph × Hacker House Goa 2026 challenge, on the IEEE-CIS
(Vesta) card-fraud dataset.*

Fraud analysts don't lack data — they lack time. Every alert means pulling a
card's history, tracing connected accounts, checking devices and regions,
reading policy, and writing it all up before deciding. This project is an AI
agent that does that investigation on a TigerGraph knowledge graph and comes
back with a verdict, an exposure figure, a policy-compliant next action, and —
where the rules require it — a suspicious activity report.

The dataset has a deliberate twist: **there is no fraud label.** The only
ground truth is 5,565 closed historical investigations. The 20 exam cases are
unlabeled and graded against a hidden key. You have to decide each one from
evidence alone.

## What I built

For every alert, one investigation runs a fixed loop:

```
TRIGGER → GATHER → RETRIEVE → ASSESS
   → snapshot the initial recommendation → REQUEST EVIDENCE → RE-ASSESS
   → DECIDE → EXPLAIN → PERSIST
```

It gathers the transaction and the cardholder's baseline from the graph,
retrieves connected prior cases and the relevant policy via GraphRAG, assesses
the situation, records what it would do *before* asking for more evidence,
simulates the cardholder's response, re-assesses, and then commits to a final
recommendation. Each case becomes one answer file: the case record, the SAR
when policy calls for one, and the next-best-action with its approval route,
both before and after the evidence request.

It's a plain Python state machine — a dataclass and one function per stage. No
agent framework. The transitions are explicit enough that a framework would
have added vocabulary, not structure.

## The decision that shaped everything: what the LLM is *not* allowed to do

About 60% of the grade is read straight out of the answer files, and roughly
half of those fields are mechanically checkable — an exposure figure that must
equal the sum of the flagged transactions, an approval route fixed by policy, a
SAR flag that must match whether `FILE_REPORT` is in the action list. A model
that guesses at those will eventually contradict itself.

So the work is split down the middle:

| The LLM decides | Code decides |
|---|---|
| verdict on ambiguous cases | exposure (sum of the episode) |
| fraud probability, within anchored bands | whether a report is filed |
| the evidence wording, the case summary | every approval route |
| the SAR narrative | the action set (rules R1–R10) |
| *confirming* the pattern | *proposing* the pattern |
| | the stop condition, the case status |

This wasn't a guess. Handed the four-charges-under-$500 structuring burst, the
model described it perfectly in prose — "four online charges of $450–499 within
thirty minutes, consistent with structuring" — and then labeled it
`card_not_present_fraud`. Reasoning about evidence and picking a label from a
closed vocabulary are different skills, and only one of them needs a model. The
label is a rule; the model explains it.

Every invariant is enforced at construction with Pydantic, so an answer file
that contradicts itself literally cannot be built. When the model's probability
comes back clustered at 0.9 (which it does, every time, un-anchored), it's
clamped into the band its evidence supports.

## How TigerGraph is used

The graph is the investigation surface, not a bucket the agent dumps data into
and reads back. Eight GSQL queries do the work, and one of them is the whole
reason this project has a story.

### Finding a ring without being told one exists

Two of the confirmed historical patterns are undocumented rings. One is a
device ring: a single phone, behind an anonymising proxy, used to make small
purchases on dozens of unrelated cards. The obvious way to find it — count how
many cards share each device profile — **does not work.** Over the exam window,
the most-shared profiles are:

```
367 cards   unknown | unknown | unknown | unknown         (missing data)
251 cards   Windows | Windows 10 | chrome 65.0            (a popular browser)
183 cards   iOS Device | iOS 11.3.0 | mobile safari       (a popular phone)
 28 cards   <the actual ring>                             — ranked 71st
```

Popularity is not conspiracy. What separates the ring is two behavioural
ratios: how often the device was *new to the account* using it, and how often
it sat behind an *anonymising proxy*. A genuinely shared device — a family
tablet, an office desktop — has returning users and no proxy. The ring is new
to **every** account it touches and proxied on **every** transaction. Ranking
by `cards × new_ratio × anon_ratio` puts it first, at a 4× margin over the next
candidate — with no device string anywhere in the query. `ring_detect` finds it
as a property of the portfolio, not something hard-coded.

The other queries are quieter but carry the verdicts: `customer_baseline`
(is this consistent with how this card is used?), `card_window` (the episode),
`device_neighbors`, `region_history` (a trip or a cloned card?), and
`similar_closed_cases` (prior cases connected by shared entity).

### GraphRAG that retrieves a rule, not a blob

The fraud policy is split per-rule and embedded into TigerGraph's vector store
alongside the pattern typologies and case history. Retrieving "the policy" as
one blob would be useless — it's thousands of words and the model still has to
find the relevant rule. Instead, the situation *"customer disputes a charge
they make regularly; should we block the card?"* retrieves **R7 (disputed but
legitimate) at 0.79**, then R2, then the false-alarm calibration note — the
governing rule and the argument *against* acting, together. Vectors live in
TigerGraph; similarity runs in GSQL. The whole retrieval surface — plus every
query above — is also served over the **Model Context Protocol**, so the
agent's graph capabilities are real MCP tools.

## The agentic capabilities

- **Uncertainty handling.** A weak single signal doesn't get a block; it gets a
  verification request. The recommendation is snapshotted before evidence and
  again after, and the two differ when the assumed response moves the needle —
  which is exactly what the policy asks to be shown.
- **The R7 trap.** The closed history contains 900 cleared cases and **zero**
  are customer disputes — every one is a model false alarm. An agent that
  learns its prior from history concludes "dispute = fraud" and blocks the very
  cases R7 exists to protect. The recurrence check runs against the graph, not
  precedent: a charge the cardholder has made 170 times before is a charge they
  forgot they authorise, not fraud.
- **Chronological case memory.** The pack is processed in `opened_at` order, so
  the ring case lands fourth and the sixteen investigations after it can
  retrieve it from the graph and cite it. The agent writes its own closed cases
  back as vertices — memory that grows across the run.
- **Policy as code.** Routes, the SAR decision, and the R1–R10 action sets are
  deterministic and reproduce the organisers' one worked example exactly.

The final distribution on the 20 cases: 7 fraud, 12 legitimate, 1 uncertain;
both rings found; 3 SARs filed including a case sitting *exactly* on the
$1,000.03 reporting boundary; zero over-blocking.

## What I learned

The lesson that repeated itself, five times, was this: **every serious bug
reported success while doing nothing.** In order of appearance:

1. The `card_id` reconstruction rule I was confident in scored 20/20 on the
   exam cases — and was *wrong*, failing 309 of the closed cases. The exam set
   was too small to distinguish the real rule (`(card1, card4, card6)`
   lexicographic) from a plausible impostor.
2. The graph load reported perfect vertex counts — 590,742 transactions — and
   was completely empty. The REST upsert API wants attribute values wrapped as
   `{"value": ...}`; given bare values it creates the vertex and silently
   defaults every field. Every timestamp was 1970.
3. A device query returned nothing because `urlencode` renders a space as `+`
   and TigerGraph matched it literally — the profile key could never equal the
   stored one.
4. The ring detector's first threshold admitted a generic corporate browser
   (IE11-on-Windows-7, 138 cards) as a "ring" and mislabeled a legitimate
   recurring-charge case.

None of these would have been caught by "did it return something?" All were
caught by asserting against facts known independently — this transaction is
$482.12 on *that* card; the ring spans exactly 28 cards; R7 must retrieve
first. The test suite is built almost entirely from ground truth, not from the
code's own output, and that is the only reason the submission is trustworthy.

## What I'd improve with more time

- **Bulk loading.** I loaded via batched REST upserts (resumable, which mattered
  when the free-tier workspace auto-suspended mid-run). The canonical path is a
  GSQL loading job — 600K rows in minutes rather than twenty. It works; it's
  just not the fast road.
- **Holdout coverage.** The scoring harness reshapes October closed cases into
  exam-shaped alerts, but every cleared case there is a risk-score false alarm,
  so it can't test the "customer disputes something legitimate" path — exactly
  the R7 case that matters most. That path is unit-tested against ground truth
  instead, but I'd rather measure it.
- **Model routing.** Reasoning runs on a free, fast open model; the deterministic
  fields never touch it. With budget I'd A/B the SAR narratives against a
  stronger model, since explainability is the one place prose quality shows up
  in the grade.

The strongest thing I can say about the result is that it moves from an
uncertain signal to a defensible action and shows its work at every step — and
that when it's unsure, it says so and asks, instead of blocking a customer who
did nothing wrong.

*Code: github.com/Asheesh7298/tigergraph-fraud-agent*
