# Demo video plan (~4:00)

**Audience: the hackathon creator.** He knows the task, the dataset, and that
there's no fraud label — so the video does **not** explain the problem or tour
features. It explains **how it's built and how it works**: the architecture,
the data flow, the engineering decisions. The deck (`ui/demo.html`) carries
that; the terminal appears only as live proof the internals are real.

Deck has 7 scenes, advanced manually (→ / Space): 1 Title · 2 Pipeline ·
3 Architecture flowchart · 4 Approach · 5 Ring algorithm (2-step) · 6 Stack ·
7 Close.

Split: ~2:45 deck (how it's built), ~1:00 live proof. Concept-heavy by design.

---

## Timeline

| Time | Screen | Voiceover beat | Notes |
|---|---|---|---|
| 0:00–0:12 | **Deck 1** title | "How we built an agentic fraud investigator on TigerGraph." | Short — no problem setup, he knows it |
| 0:12–0:45 | **Deck 2** pipeline | "One investigation runs nine explicit stages — gather, retrieve, assess, request evidence, re-assess, decide, explain, persist. A plain Python state machine, no framework." | Walk the stages |
| 0:45–1:25 | **Deck 3** architecture flowchart | "Data flows down the stack: narrowed CSVs into TigerGraph — graph plus a vector store — reached through GSQL and GraphRAG, exposed over MCP, driven by the agent with the LLM alongside, out to the answer files and the dashboard." | **The centerpiece he asked for.** Trace each layer as it builds |
| 1:25–1:55 | **Deck 4** approach | "Four things make it an agent, not a classifier: episode reconstruction, verify-before-block, cross-case graph memory, and GraphRAG policy grounding." | One line per card |
| 1:55–2:30 | **Deck 5** ring algorithm (2-step) | "Ring detection is the interesting one. By shared cards the ring is rank 71. Rank by behaviour — new to every account × behind a proxy — and it's #1. No device string in the query." | Time the → press to the re-rank |
| 2:30–2:50 | **LIVE terminal** `tools.py --smoke` | "That runs for real against the live graph — 28 cards, ring-score 28 vs 7.5." | First live cut — proves the algorithm |
| 2:50–3:10 | **LIVE terminal** `mcp_server.py --selftest` | "And the whole graph surface is served over MCP — the eight tools list and call, here." | Let the OK lines land |
| 3:10–3:25 | **LIVE** (optional) GraphStudio or React UI, ~12s | "Loaded and queryable in TigerGraph; the cases render in the dashboard." | Brief proof it's a running system, not a mock — skip if tight |
| 3:25–3:45 | **Deck 6** stack + split | "The stack: TigerGraph for graph and vectors, a Python state machine with Groq and Gemini, React and 73 tests. And the split that keeps it honest — the model reasons and writes; code owns exposure, routes, the report, the rules." | |
| 3:45–4:00 | **Deck 7** close | "It shows its work, cites the query behind every claim, and asks when it's unsure instead of blocking someone who did nothing wrong." | End on the result line + repo |

---

## Record in blocks, assemble after

1. **Deck** — `ui/demo.html`, F11 fullscreen, one clean silent pass. Linger on
   scenes 3 (architecture) and 5 (ring); trim in DaVinci. On the ring scene press
   → once for the re-rank before moving on.
2. **Terminal** — big font (18pt+), Savanna workspace awake first. Run
   `python src/tools.py --smoke` and `python src/mcp_server.py --selftest`; let the
   28-card and 8-tool lines sit.
3. **Optional live** — 10–12s of GraphStudio (the loaded graph) or `cd web &&
   npm run dev` (the dashboard). Just enough to show it's a running system.
4. **Voiceover** — record last against the beats above, then cut visuals to it.

## DaVinci assembly
- Audio: voiceover. Video: deck segments + the short live clips in timeline order.
- Optional lower-thirds naming the tool on screen (`ring_detect`, MCP selftest).
- Simple cuts or a 6-frame dissolve. Export 1080p H.264.

## Open decision
Include the ~12s live GraphStudio/UI glance (3:10) or not? It reinforces "real
running system," but the terminal proof may be enough for a creator audience.
Keep it if the video is under 4:00 without it.
