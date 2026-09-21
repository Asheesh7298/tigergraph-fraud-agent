# Demo video — shot list & script (3–5 min)

Target 4:00. Screen recording with voiceover. Have the workspace **awake**
before recording (open it once so it's not mid-wake), and have `cases/`,
`streamlit run ui/app.py`, and a terminal ready.

---

### 0:00–0:25 — The problem (talking head or title card)

> "Fraud analysts don't lack data — they lack time. Every alert means pulling a
> card's history, tracing connected accounts, checking devices, reading policy,
> and writing it up before the money's gone. I built an agent that does that
> investigation on a TigerGraph knowledge graph — and the dataset has no fraud
> label, so it has to decide every case from evidence alone."

Show: the four CSVs / the README line "There is no Is Fraud flag."

### 0:25–1:00 — Architecture (one diagram)

Show the architecture: graph + vector store → MCP → agent state machine →
answer files + UI. Say the one line that matters:

> "The split that decides the score: the model reasons and writes prose, but
> exposure, approval routes, the report decision and the action rules are pure
> code — because half the graded fields are checkable, and a model that guesses
> at them contradicts itself."

### 1:00–2:00 — The ring, found without being told (the money shot)

Terminal:
```
python src/tools.py --smoke
```
Point at the `ring_detect` lines: **28 cards, new_ratio 1.0, anon_ratio 1.0,
ring_score 28.0 vs 7.5** for the next cluster.

> "Counting cards per device doesn't find the ring — the top profiles are
> missing data and popular browsers, and the real ring sits at rank 71. What
> finds it is behaviour: a device new to every account it touches, behind an
> anonymising proxy. No device string in the query — the graph surfaces it."

### 2:00–2:45 — A case, end to end, in the UI

`streamlit run ui/app.py` → open **HHG-014** (the ring case):

> "Here's the analyst request — 'several cards, same unusual device.' The agent
> pulls the episode, finds the shared-device ring across 28 cards, files a SAR
> on the shared-origin rule, and — crucially — shows the recommendation before
> and after it asked the cardholder. Every claim cites the graph query behind
> it."

Then open **HHG-018** (R7):

> "And restraint: this customer disputed a $39 charge — but they've made that
> exact charge 170 times. The agent recognises their own recurring pattern and
> does *not* block. Half the cases here are legitimate; an agent that blocks
> everything fails."

### 2:45–3:20 — MCP + GraphRAG

Terminal:
```
python src/mcp_server.py --selftest
```
> "The graph capabilities are served over MCP — the same eight tools the agent
> uses. And GraphRAG grounds it: ask 'customer disputes a recurring charge' and
> the policy's rule R7 comes back first, with the note arguing against blocking."

### 3:20–3:50 — Results

Show the run summary / distribution:

> "Across the twenty cases: seven fraud, twelve legitimate, one uncertain. Both
> rings found. Three reports filed — including one sitting exactly on the
> $1,000 boundary. Zero schema violations, every ID real."

### 3:50–4:00 — Close

> "From an uncertain signal to a defensible action, with its work shown at every
> step — and when it's unsure, it asks instead of blocking. Built on TigerGraph."

---

**Recording tips**
- Pre-run `python src/tools.py --smoke` once so the workspace is awake; the live
  take is then fast.
- Font size up in the terminal; the ring numbers must be legible.
- If a live call is slow, cut to the already-written `cases/HHG-014.json`.
- Keep it under 5:00 — the brief says 3–5.
