# Social post — tag @TigerGraphDB AND @247pmstudio

The form requires tagging **both** handles, and **every team member posts**
(paste every post link in the form). Pick a template, add the blog/demo link
and a screenshot (the `ring_detect` smoke-test output, or the UI on HHG-014).

---

## LinkedIn (longer)

Just built an agentic fraud investigator on @TigerGraph for the Hacker House
Goa 2026 challenge, and the most interesting part was what the graph found that
a risk model couldn't.

The dataset (IEEE-CIS card fraud) has no fraud label — the agent has to decide
each case from evidence. It runs the whole investigation on a TigerGraph
knowledge graph: pulls the transaction episode, the cardholder's baseline,
connected prior cases, and grounds its reasoning with GraphRAG over the fraud
policy stored as vectors in TigerGraph.

The highlight: it detects a device-fraud ring **without being told one exists**.
Counting cards per device doesn't work — the most-shared profiles are just
popular browsers, and the real ring ranks 71st. What surfaces it is behaviour:
a device new to every account it touches, behind an anonymising proxy. Ranked
that way it jumps to #1 — 28 cards, no device string in the query.

The other design choice that mattered: the LLM reasons and writes, but exposure,
approval routes, the report decision and the policy rules are pure code. Half
the graded fields are checkable, and a model that guesses at them contradicts
itself.

Also served the whole graph surface over MCP. Wrote up the build — link below.

@TigerGraphDB @247pmstudio #TigerGraph #GraphRAG #AIAgents #FraudDetection

---

## X / Twitter (thread)

**1/**
Built an agentic fraud investigator on @TigerGraphDB for #HackerHouseGoa.
The dataset has no fraud label — the agent decides every case from evidence on
a knowledge graph. 🧵

**2/**
The best part: it finds a device-fraud ring *without being told one exists*.
Counting shared devices fails — top profiles are just popular browsers, real
ring ranks 71st.
Rank by "new to every account × behind a proxy" → jumps to #1. 28 cards, no
device string in the query.

**3/**
Key design call: the LLM reasons and writes prose, but exposure, approval
routes, the SAR decision and the policy rules are pure code.
Half the graded fields are checkable — a model that guesses contradicts itself.

**4/**
GraphRAG over the fraud policy stored in TigerGraph: ask "customer disputes a
recurring charge" → the exact governing rule comes back first, with the note
arguing *against* blocking. Whole graph surface also served over MCP.

**5/**
From an uncertain signal to a defensible action, work shown at every step — and
when it's unsure it asks instead of blocking a customer who did nothing wrong.
Write-up + code: [link]
@TigerGraphDB @247pmstudio #GraphRAG #AIAgents
