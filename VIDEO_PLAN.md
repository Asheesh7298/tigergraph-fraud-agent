# Full demo video plan (~4:00)

**Principle:** the deck (`ui/demo.html`) carries the narrative — approach and
architecture are the differentiators, so they lead. The live app and terminal
appear as *proof* that the deck's claims are real. Target 3:45–4:00 (brief is
3–5 min).

Deck has 7 scenes, advanced manually (→ / Space): 1 Title · 2 Constraint ·
3 Architecture · 4 Approach · 5 Ring reveal (2-step) · 6 Results · 7 Close.

---

## Timeline

| Time | Screen | Voiceover beat | Notes |
|---|---|---|---|
| 0:00–0:16 | **Deck 1–2** (title → constraint) | "Fraud teams lack time, not data. This dataset has no fraud label — 20 cases to decide from evidence alone." | Open cold on the title |
| 0:16–0:42 | **Deck 3** (architecture) | "The graph investigates; code decides the rules — routes, exposure, the report decision. The model only reasons and writes the prose." | Let the pipeline + split build in |
| 0:42–1:18 | **Deck 4** (approach — the 4 cards) | "What makes it an agent: it rebuilds the whole episode, verifies before it blocks, remembers cases across the run, and grounds every call in the policy via GraphRAG." | This is *our approach* — give it room; one sentence per card |
| 1:18–1:48 | **Deck 5** (ring reveal, 2-step) | "The finding — a ring the risk model can't see. By shared cards it's rank 71. Rank by behaviour — new to every account, behind a proxy — and it's #1." | Time your → press to the re-rank |
| 1:48–2:05 | **LIVE terminal** `tools.py --smoke` | "Not a slide — live: 28 cards, ring-score 28 vs 7.5, and no device string in the query." | First real footage; proves the deck |
| 2:05–2:45 | **LIVE website** — HHG-014 | "A case end to end: the episode, the 28 connected cards, the report filed, and the recommendation before and after asking the customer." | The website payoff. Scroll evidence → initial/final → SAR |
| 2:45–3:05 | **LIVE website** — HHG-018 | "And restraint: a $39 charge the customer's made 170 times — it recognises their own pattern and does not block." | Second case shows range (drop if tight) |
| 3:05–3:20 | **LIVE terminal** `mcp_server.py --selftest` | "The whole graph surface is served over MCP — the eight tools the agent uses." | Quick, let the OK lines land |
| 3:20–3:42 | **Deck 6** (results) | "Seven fraud, twelve legitimate, one uncertain. Both rings. Three reports, including one exactly on the $1,000 line. Zero schema errors." | Counters animate |
| 3:42–4:00 | **Deck 7** (close) | "From an uncertain signal to a defensible action — and when unsure, it asks instead of blocking someone who did nothing wrong." | End on the repo URL |

**Split:** ~2:20 deck (approach/architecture/ring/results), ~1:20 live proof.
The concept leads; the product proves it.

---

## Record in blocks, assemble after

1. **Deck** — `ui/demo.html`, F11 fullscreen, one clean silent pass. Pause on
   each scene longer than needed; trim in DaVinci. Advance with →; on the ring
   scene press → once for the re-rank before moving on.
2. **Website** — `cd web && npm run dev`, fullscreen the browser. Open HHG-014,
   scroll slowly through evidence / initial-vs-final / SAR. Then HHG-018. Move
   the cursor deliberately — fast cursor motion reads as nervous on video.
3. **Terminal** — big font (18pt+). Run `python src/tools.py --smoke` and
   `python src/mcp_server.py --selftest`. Let the OK/28-card lines sit on screen.
   Make the Savanna workspace awake first (open it once) so nothing stalls.
4. **Voiceover** — record last against the beats above, then cut visuals to it.

## DaVinci assembly
- Audio track: voiceover.
- Video track 1: deck segments + live footage in the timeline order above.
- Optional lower-thirds: `card_window`, `ring_detect`, `search_documents` as
  captions when the matching tool is on screen.
- Keep transitions simple (cut or a 6-frame dissolve). Export 1080p, H.264.

## Two open decisions
1. **Website placement** — plan puts it at 2:05 (payoff after the concept).
   Alternative: show it at 0:20 as "here's the product," then explain. Payoff
   placement recommended.
2. **Two cases or one** — HHG-014 + HHG-018 shows range in ~60s; HHG-014 alone
   carries it if you're tight.
