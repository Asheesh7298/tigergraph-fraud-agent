# Agentic Fraud Investigation on TigerGraph

An AI agent that investigates card-fraud alerts on a TigerGraph knowledge
graph, decides what kind of fraud each is (if any), how far it reaches, and
what the bank should do next — including when to gather more evidence before
deciding. Built for the TigerGraph × Hacker House Goa 2026 challenge on the
IEEE-CIS (Vesta) dataset.

The dataset has **no fraud label**. The only ground truth is 5,565 closed
historical investigations; the 20 exam cases are deliberately unlabelled and
scored against a hidden key. The task is to reach a defensible verdict on each
from evidence alone.

## What it does

For each alert the agent runs one investigation:

```
TRIGGER → GATHER → RETRIEVE → ASSESS
   → snapshot initial actions → REQUEST EVIDENCE → RE-ASSESS
   → DECIDE → EXPLAIN → PERSIST
```

and writes one answer file (`cases/HHG-0NN.json`) containing the case record,
a suspicious activity report where policy requires one, and the next best
action with its approval route — recorded both **before** and **after** the
evidence the agent chose to request.

## The design decision that matters

Roughly 60% of the score is read straight out of the JSON files, and about
half of those fields are mechanically checkable. So the work is split:

| Decided by the LLM | Decided by code |
|---|---|
| verdict, on ambiguous cases | exposure (sum of the episode) |
| fraud probability (within anchored bands) | whether a report is filed |
| evidence wording, case summary | every approval route |
| the SAR narrative | the action set (rules R1–R10) |
| confirming the pattern | proposing the pattern |
| | the stop condition, the case status |

The LLM reasons and writes; it never decides a field that can contradict
another. Tested empirically: given the Ring B burst, the model described it
correctly — "four charges of $450–499 in thirty minutes" — and then labelled
it `card_not_present_fraud`. Reasoning and labelling are different skills, and
the label is a rule.

## How TigerGraph is used

The graph is the investigation surface, not a store the agent dumps and reads
back. Eight GSQL queries do the work (`graph/queries/`):

- **`ring_detect`** — the innovation play. It finds a fraud ring **without
  being told one exists** and with no device string in the query. Counting
  cards per device profile does not work: the most-shared profiles are missing
  data and popular browsers, and the real ring sits at rank 71 by that
  measure. Ranking instead by `cards × new-to-account ratio × anonymising-proxy
  ratio` puts it first — 28 cards, new to every account it touched, behind a
  proxy on every transaction, a 4× margin over the next candidate. A genuinely
  shared device (a family tablet) has returning users; this one never does.
- **`card_window` / `customer_baseline`** — the episode and the cardholder's
  normal behaviour. Nearly every verdict turns on "is this consistent with how
  this card is used".
- **`device_neighbors` / `region_history`** — who else is on a device, and
  whether a card has ever billed in a region before.
- **`similar_closed_cases`** — case memory by *connected entity* (same card,
  device, region), complemented by
- **`search_documents` / `search_closed_cases`** — GraphRAG vector search over
  the fraud policy, the pattern typologies and the case history, stored and
  compared inside TigerGraph.

Cases the agent closes are written back as `AgentCase` vertices, and because
the pack is processed in chronological order, later investigations retrieve
earlier ones as memory.

### TigerGraph MCP — used at runtime, not just exposed

The agent reaches the graph **through** MCP. `src/mcp_server.py` serves the
graph operations as MCP tools (the GSQL queries, transaction and device
lookups, GraphRAG document and case search, and case persistence); at run time
`src/mcp_tools.py` launches that server as a stdio subprocess and the agent
calls it, so the live chain is:

```
agent → MCP client → mcp_server.py → GSQL / REST → TigerGraph
```

Every graph read and the case write-back go over MCP — there is no direct graph
path in the agent (a `--no-mcp` flag exists only for debugging). Vector search
takes plain text and embeds it server-side, so no query vector crosses the
boundary.

```bash
python src/mcp_server.py --selftest   # list + exercise all 13 tools
python src/run_cases.py --all          # the agent runs through MCP
```

Register the server with any MCP client (Claude Desktop, the mcp inspector)
using `mcp_config.example.json`.

## Layout

```
src/
  card_id.py       reconstruct the card_id the pack uses (not a source column)
  prep_data.py     narrow transactions 397 → 59 columns, build the id registry
  graph_client.py  TigerGraph connection (Savanna JWT or Community Edition)
  create_schema.py / load_graph.py / create_queries.py / build_vectors.py
  schema.py        answer-file models; every invariant enforced at construction
  policy.py        deterministic engine: routes, SAR decision, actions R1–R10
  classify.py      pattern classifier and episode expansion
  tools.py         the eight queries wrapped as the agent's tool surface
  llm.py           Gemini access, disk cache, rate-limit pacing
  agent.py         the investigation state machine
  run_cases.py     run the pack, write cases/HHG-0NN.json
graph/             schema.gsql, vectors.gsql, queries/*.gsql
docs/              GraphRAG corpus + DATASET.md (the organisers' spec + policy)
holdout/           build a scoring set from October history; score against it
ui/app.py          read-only Streamlit case viewer
tests/             73 tests, incl. the README worked example and both rings
cases/             the twenty answer files (the deliverable)
```

## Running it

Requires Python 3.11+, a TigerGraph workspace (Savanna free tier or Community
Edition), and a Gemini API key.

```bash
pip install -r requirements.txt

# put the four dataset CSVs in data/raw/ , then:
cp .env.example .env          # fill in TG_HOST, TG_SECRET, TG_GRAPH, GEMINI_API_KEY

python src/prep_data.py       # narrow the data, build the card map + id registry
python src/create_schema.py   # create the FraudGraph schema
python src/load_graph.py      # load it (resumable; ~20 min)
python src/create_queries.py  # install the GSQL queries
python src/build_vectors.py --docs   # embed the policy + typology corpus

python src/run_cases.py --all # investigate all 20, write cases/
python -m pytest tests/       # 73 tests
```

Browse the results in the analyst dashboard:

```bash
cd web && npm install && npm run dev   # React frontend (Vite) at localhost:5173
```

There is also a lightweight Streamlit viewer (`streamlit run ui/app.py`) over
the same `cases/*.json`. The React app bundles the case data, so it needs no
backend to run.

Holdout scoring, when tuning:

```bash
python holdout/build_holdout.py --n 30
python src/run_cases.py --all --pack holdout/holdout_pack.csv --out holdout/answers
python holdout/score.py
```

## Stack

TigerGraph Savanna (graph + vector store) · GSQL · Google Gemini
(`gemini-3.5-flash` for reasoning, `gemini-embedding-001` for retrieval) ·
Pydantic · Streamlit. Plain-Python state machine, no agent framework — the
transitions are explicit enough that a framework would add vocabulary rather
than structure.

## Notes on the data

The card IDs the pack uses (`C12382-K1`) are not a column in the source. They
reconstruct as `(card1, card4, card6)` sorted lexicographically within a
customer — verified 20/20 on the pack and 4665/4665 on the closed history.
`V1`–`V339` (Vesta's unnamed engineered features) are dropped: they cannot be
honestly cited as evidence to an analyst or a regulator, and an attribute we
would never cite does not belong in the graph that serves evidence.
