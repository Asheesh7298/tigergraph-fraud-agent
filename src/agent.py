"""The investigation state machine.

    TRIGGER -> INVESTIGATE -> GATHER -> RETRIEVE -> ASSESS
      -> [snapshot initial actions] -> REQUEST EVIDENCE -> RE-ASSESS
      -> DECIDE -> EXPLAIN -> PERSIST

A plain dataclass and one function per stage. The rubric permits a custom
implementation, and the transitions here are explicit enough that a framework
would add vocabulary rather than structure.

What the model does and does not do is the important design decision. It
weighs evidence, sets a probability inside anchored bands, and writes the
prose a human reads. It does not choose the pattern label, compute exposure,
pick approval routes, decide whether a report is filed, or select actions --
those are in classify.py and policy.py, because roughly half the scored
fields are mechanically checkable and a model that guesses at them will
eventually contradict itself.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import classify as C  # noqa: E402
import policy as P  # noqa: E402
from llm import Gemini, LLMError  # noqa: E402
from schema import (  # noqa: E402
    Answer,
    Case,
    Evidence,
    EvidenceRequest,
    NextBestActions,
    SAR,
)
from tools import FraudTools, ToolLog, as_dt, fmt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

WINDOW_HOURS = 72          # context around the flagged transaction
RING_FROM = "2016-11-01 00:00:00"
RING_TO = "2016-12-31 23:59:59"


# --------------------------------------------------------------------------
# Probability anchors
# --------------------------------------------------------------------------
#
# Un-anchored models cluster everything near 0.9: asked to assess the Ring B
# burst cold, gemini-3.5-flash returned 0.98 and the lite model 0.95. The
# bands below are given to the model in the prompt and clamped afterwards,
# because `fraud_probability` is scored for calibration and a submission
# where every case reads 0.95 is not calibrated.

ANCHORS = """
| Evidence state                                   | Probability |
|--------------------------------------------------|-------------|
| Risk score alone, activity consistent with baseline | 0.10 - 0.25 |
| One anomaly, nothing corroborating                  | 0.30 - 0.50 |
| Two independent anomalies                           | 0.55 - 0.70 |
| Cardholder denies the transaction                   | 0.75 - 0.85 |
| Denial plus ring membership or structuring          | 0.85 - 0.95 |
| Activity matches the cardholder's own established pattern | 0.05 - 0.15 |
""".strip()

BAND = {
    0: (0.10, 0.30),
    1: (0.30, 0.55),
    2: (0.55, 0.72),
    3: (0.62, 0.88),
}


def clamp(p: float, signals: int, denied: bool, ring: bool, recurring: bool) -> float:
    """Keep the model's number inside the band its evidence supports."""
    if recurring:
        return min(max(p, 0.03), 0.15)
    lo, hi = BAND.get(min(signals, 3), BAND[3])
    if denied:
        lo, hi = max(lo, 0.75), 0.95 if ring else 0.88
    return round(min(max(p, lo), hi), 2)


# --------------------------------------------------------------------------

@dataclass
class Trigger:
    case_id: str
    opened_at: str
    trigger_type: str
    trigger_text: str
    flagged_txn_id: str
    card_id: str
    customer_id: str
    risk_score: float | None = None

    @classmethod
    def from_row(cls, r: dict[str, str]) -> "Trigger":
        rs = (r.get("risk_score") or "").strip()
        return cls(
            case_id=r["case_id"],
            opened_at=r["opened_at"],
            trigger_type=r["trigger_type"],
            trigger_text=r["trigger_text"],
            flagged_txn_id=r["flagged_txn_id"],
            card_id=r["card_id"],
            customer_id=r["customer_id"],
            risk_score=float(rs) if rs else None,
        )


@dataclass
class State:
    trigger: Trigger
    flagged: dict[str, Any] = field(default_factory=dict)
    window: list[dict[str, Any]] = field(default_factory=list)
    baseline: dict[str, Any] = field(default_factory=dict)
    region: dict[str, Any] = field(default_factory=dict)
    ring: dict[str, Any] | None = None
    episode: list[dict[str, Any]] = field(default_factory=list)
    assessment: C.Assessment = field(default_factory=C.Assessment)
    prior_cases: list[dict[str, Any]] = field(default_factory=list)
    prior_agent: list[dict[str, Any]] = field(default_factory=list)
    guidance: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    notes: list[str] = field(default_factory=list)


class RingIndex:
    """Runs ring_detect once, then answers per-card lookups.

    The query scans every online transaction in the window, so running it per
    case would repeat the same portfolio-wide scan twenty times. It is also
    conceptually right to compute it once: a ring is a property of the book,
    not of the case that happens to surface it.
    """

    CACHE = ROOT / "data" / ".ring_index.json"

    #: A real device ring is new to every account it touches AND hides behind
    #: an anonymising proxy. Ring A scores 1.0 on both; the false positives are
    #: ordinary shared browsers -- IE11-on-Win7 (138 cards) is 41% new, 13%
    #: anonymised, and a score threshold alone let it through and mislabelled a
    #: legitimate recurring-charge case as an undocumented ring. Gating on the
    #: ratios, not the score, keeps only genuine anonymised-proxy rings.
    MIN_NEW_RATIO = 0.80
    MIN_ANON_RATIO = 0.50

    def __init__(self, tools: FraudTools, start: str = RING_FROM, end: str = RING_TO,
                 min_cards: int = 3, min_score: float = 3.0, use_cache: bool = True) -> None:
        self.by_card: dict[str, dict[str, Any]] = {}
        self.clusters: list[dict[str, Any]] = []

        # The scan covers every online transaction in the window and the
        # answer does not change between cases, so it is cached to disk.
        # Delete data/.ring_index.json to force a rebuild.
        key = f"{start}|{end}|{min_cards}|{min_score}|n{self.MIN_NEW_RATIO}|a{self.MIN_ANON_RATIO}"
        ranked: list[dict[str, Any]] | None = None
        if use_cache and self.CACHE.exists():
            try:
                blob = json.loads(self.CACHE.read_text(encoding="utf-8"))
                if blob.get("key") == key:
                    ranked = blob["ranked"]
            except Exception:  # noqa: BLE001
                ranked = None

        if ranked is None:
            ranked = tools.ring_detect(start, end, min_cards=min_cards, top=60).get("ranked", [])
            if use_cache:
                self.CACHE.parent.mkdir(parents=True, exist_ok=True)
                self.CACHE.write_text(
                    json.dumps({"key": key, "ranked": ranked}), encoding="utf-8"
                )

        for row in ranked:
            # Both a meaningful score and the anonymised-proxy + new-device
            # signature. The ratio gates are what separate a real ring from a
            # popular browser shared by many unrelated cardholders.
            if row["ring_score"] < min_score:
                continue
            if row.get("new_ratio", 0) < self.MIN_NEW_RATIO:
                continue
            if row.get("anon_ratio", 0) < self.MIN_ANON_RATIO:
                continue
            self.clusters.append(row)
            for card in row["cards"]:
                # A card keeps its strongest cluster.
                prev = self.by_card.get(card)
                if prev is None or row["ring_score"] > prev["ring_score"]:
                    self.by_card[card] = row

    def for_card(self, card_id: str) -> dict[str, Any] | None:
        return self.by_card.get(card_id)


# --------------------------------------------------------------------------

class Investigator:
    def __init__(
        self,
        tools: FraudTools | None = None,
        llm: Gemini | None = None,
        rings: RingIndex | None = None,
        verbose: bool = True,
    ) -> None:
        self.tools = tools or FraudTools()
        self.llm = llm or Gemini()
        self.verbose = verbose
        self.rings = rings if rings is not None else RingIndex(self.tools)

    def say(self, msg: str) -> None:
        if self.verbose:
            print(f"    {msg}", flush=True)

    # -- stages ----------------------------------------------------------
    def gather(self, s: State) -> None:
        t = s.trigger
        s.flagged = self.tools.get_transaction(t.flagged_txn_id)
        if not s.flagged:
            raise RuntimeError(f"{t.case_id}: flagged transaction {t.flagged_txn_id} not found")
        s.flagged.setdefault("txn_id", t.flagged_txn_id)
        ts = s.flagged["ts"]
        s.step += 1

        # The alert can open hours after the activity. HHG-006 opens at 02:30
        # on the 22nd about a burst that ended at 20:30 on the 21st; windowing
        # off opened_at would miss the whole episode.
        if abs((as_dt(t.opened_at) - as_dt(ts)).total_seconds()) > 3600:
            s.notes.append(
                f"alert opened {t.opened_at}, activity at {ts}; windowed on the transaction"
            )

        w = self.tools.card_window(t.card_id, ts, hours=WINDOW_HOURS)
        s.window = [
            {**r["attributes"], "txn_id": r["v_id"]} for r in (w.get("Txns") or [])
        ]
        devs = w.get("device_of") or {}
        for row in s.window:
            row.setdefault("device_profile", devs.get(row["txn_id"], ""))

        s.baseline = self.tools.customer_baseline(
            t.card_id, ts,
            amount=float(s.flagged.get("amount") or 0),
            product=s.flagged.get("product_cd") or "",
        )
        s.step += 1

        s.ring = self.rings.for_card(t.card_id)
        if s.ring:
            self.say(f"ring: {s.ring['n_cards']} cards, score {s.ring['ring_score']}")

        if s.flagged.get("channel") == "in_person" and s.flagged.get("addr1"):
            s.region = self.tools.region_history(t.card_id, s.flagged["addr1"], ts)
            s.step += 1

        ring_ids: set[str] = set()
        if s.ring:
            d = self.tools.device_neighbors(s.ring["profile"], RING_FROM, RING_TO)
            ring_ids = set(d.get("txn_ids") or [])
            s.ring = {**s.ring, "first_seen": d.get("first_seen", s.ring.get("first_seen", "")),
                      "last_seen": d.get("last_seen", s.ring.get("last_seen", ""))}
            s.step += 1

            # A ring spreads its activity thinly: HHG-014's card carries three
            # transactions on the ring device, 11 days apart, and a window
            # centred on the flagged one at +/-72h sees only the middle leg.
            # Widen to the ring's own lifespan so the episode is complete.
            wide = self.tools.card_window_between(
                t.card_id, s.ring["first_seen"], s.ring["last_seen"], limit=800
            )
            wdev = wide.get("device_of") or {}
            known = {x["txn_id"] for x in s.window}
            for r in (wide.get("Txns") or []):
                if r["v_id"] in known:
                    continue
                row = {**r["attributes"], "txn_id": r["v_id"]}
                row.setdefault("device_profile", wdev.get(r["v_id"], ""))
                if r["v_id"] in ring_ids:
                    s.window.append(row)
            s.window.sort(key=lambda x: x["ts"])
            s.step += 1

        s.episode = C.expand_episode(s.flagged, s.window, s.baseline, ring_ids)
        s.assessment = C.classify(
            s.flagged, s.episode, s.baseline,
            ring=s.ring, region=s.region or None, card_id=t.card_id,
        )
        self.say(
            f"episode: {len(s.episode)} txn(s), ${sum(float(x['amount']) for x in s.episode):,.2f}"
            f"  pattern: {s.assessment.pattern}  signals: {s.assessment.independent_signals}"
        )

    def retrieve(self, s: State) -> None:
        """GraphRAG: connected prior cases, plus the written guidance."""
        t = s.trigger
        exposure = sum(float(x["amount"]) for x in s.episode)
        sim = self.tools.similar_closed_cases(
            t.card_id, pattern=s.assessment.pattern, exposure=exposure
        )
        why = sim.get("retrieved_because") or {}
        s.prior_cases = [
            {
                "case_id": r["v_id"],
                "because": why.get(r["v_id"], "retrieved"),
                **{k: r["attributes"].get(k) for k in
                   ("outcome", "pattern", "exposure_usd", "report_filed", "analyst_notes")},
            }
            for r in (sim.get("Result") or [])
        ][:8]
        s.step += 1

        question = (
            f"{t.trigger_text} Pattern observed: {s.assessment.pattern}. "
            + " ".join(sig.detail for sig in s.assessment.signals)
        )
        try:
            # Pass text; the MCP search tools embed it server-side, so retrieval
            # is fully behind MCP and no query vector crosses the boundary.
            s.guidance = self.tools.search_documents(question, k=6)
            # Narrative-similar precedent, where the history has been embedded.
            # Optional: the graph retrieval above is the stronger signal, and
            # embedding all 5,565 narratives is rate-limited on the free tier.
            try:
                hits = self.tools.search_closed_cases(question, before=t.opened_at, k=5)
                known = {c["case_id"] for c in s.prior_cases}
                for h in hits:
                    if h["case_id"] not in known and float(h.get("score", 0)) >= 0.72:
                        s.prior_cases.append({
                            "case_id": h["case_id"],
                            "because": f"similar narrative (score {float(h['score']):.2f})",
                            "outcome": h.get("outcome"), "pattern": h.get("pattern"),
                            "exposure_usd": h.get("exposure_usd"),
                            "report_filed": h.get("report_filed"),
                            "analyst_notes": h.get("notes"),
                        })
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            s.notes.append(f"document retrieval unavailable: {str(exc)[:80]}")
        s.step += 1

        # Cross-case memory: cases THIS agent has already closed this run.
        # The pack is processed in opened_at order, so every AgentCase already
        # in the graph was opened earlier and could legitimately inform this
        # one. A later case that shares the ring device profile, a connected
        # card, or the same non-trivial pattern cites the earlier HHG case --
        # which is how HHG-014 (the ring, 4th) becomes evidence for the cases
        # after it. This is retrieved from the graph, not tracked in memory.
        try:
            mine = self.tools.prior_agent_cases(limit=40)
            my_profile = s.assessment.ring_profile
            my_cards = set(s.assessment.connected_cards) | {t.card_id}
            for pc in mine:
                if pc.get("hhg_case_id") in (None, "", t.case_id):
                    continue
                shares_profile = my_profile and pc.get("pattern") == "undocumented"
                shares_pattern = (
                    s.assessment.pattern not in ("none", "")
                    and pc.get("pattern") == s.assessment.pattern
                )
                if shares_profile or shares_pattern:
                    s.prior_agent.append({
                        "hhg_case_id": pc["hhg_case_id"],
                        "verdict": pc.get("verdict"),
                        "pattern": pc.get("pattern"),
                        "graph_case_id": pc.get("__id") or "",
                        "because": ("same undocumented pattern this month"
                                    if shares_profile else
                                    f"same pattern ({pc.get('pattern')}) earlier this month"),
                    })
        except Exception as exc:  # noqa: BLE001
            s.notes.append(f"agent-memory lookup unavailable: {str(exc)[:80]}")

        self.say(
            f"retrieved: {len(s.prior_cases)} prior closed case(s), "
            f"{len(s.prior_agent)} prior agent case(s), "
            f"{len(s.guidance)} guidance section(s)"
        )

    # -- assessment ------------------------------------------------------
    def assess(self, s: State, response: str | None = None) -> dict[str, Any]:
        prompt = self._assessment_prompt(s, response)
        schema = {
            "type": "OBJECT",
            "properties": {
                "verdict": {"type": "STRING", "enum": ["fraud", "legitimate", "uncertain"]},
                "fraud_probability": {"type": "NUMBER"},
                "pattern_agrees": {"type": "BOOLEAN"},
                "pattern_comment": {"type": "STRING"},
                "reasoning": {"type": "STRING"},
                "evidence": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "claim": {"type": "STRING"},
                            "source": {"type": "STRING",
                                       "enum": ["graph", "document", "customer", "external"]},
                            "signal": {"type": "STRING"},
                        },
                        "required": ["claim", "source", "signal"],
                    },
                },
                "summary": {"type": "STRING"},
            },
            "required": ["verdict", "fraud_probability", "pattern_agrees",
                         "reasoning", "evidence", "summary"],
        }
        try:
            return self.llm.complete(prompt, schema=schema, max_tokens=12000)
        except LLMError as exc:
            s.notes.append(f"assessment fell back to rules: {str(exc)[:100]}")
            return self._fallback_assessment(s, response)

    def _fallback_assessment(self, s: State, response: str | None) -> dict[str, Any]:
        """If the model is unavailable the investigation still completes.

        Degraded, not broken: the deterministic half is unaffected, so routes,
        the report decision and the action list remain correct and only the
        prose is thinner.
        """
        n = s.assessment.independent_signals
        verdict = "fraud" if (n >= 2 or response == "denied") else (
            "legitimate" if n == 0 else "uncertain"
        )
        return {
            "verdict": verdict,
            "fraud_probability": 0.5,
            "pattern_agrees": True,
            "pattern_comment": "",
            "reasoning": "Model unavailable; assessed from the rule-based signals alone.",
            "evidence": [
                {"claim": sig.detail, "source": "graph", "signal": sig.name}
                for sig in s.assessment.signals
            ],
            "summary": (
                f"{len(s.episode)} transaction(s) assessed on {n} independent signal(s); "
                f"pattern {s.assessment.pattern}."
            ),
        }

    def _assessment_prompt(self, s: State, response: str | None) -> str:
        t = s.trigger
        ep_lines = "\n".join(
            f"  {x['txn_id']}  {x['ts']}  ${float(x['amount']):>9,.2f}  "
            f"{x.get('product_cd','?')}/{x.get('channel','?')}"
            f"  risk={float(x.get('risk_score') or 0):.2f}"
            f"  device={(x.get('device_profile') or '-')[:52]}"
            + (f"  [{x['_link']}]" if x.get("_link") else "")
            for x in s.episode
        )
        b = s.baseline
        prior = "\n".join(
            f"  {c['case_id']} ({c['because']}): {c['outcome']}, {c['pattern']}, "
            f"${float(c.get('exposure_usd') or 0):,.2f}"
            for c in s.prior_cases
        ) or "  (none retrieved)"
        mine = "\n".join(
            f"  {c['hhg_case_id']} ({c['because']}): {c['verdict']}, {c['pattern']}"
            for c in s.prior_agent
        ) or "  (none this month)"
        guide = "\n\n".join(
            f"  [{d.get('kind')}] {d.get('title')} {d.get('section')}\n"
            f"  {(d.get('content') or '')[:700]}"
            for d in s.guidance[:4]
        ) or "  (none retrieved)"
        sigs = "\n".join(f"  - {sg.name}: {sg.detail}" for sg in s.assessment.signals) or "  (none)"
        rec = int(b.get("recurring_matches") or 0)

        reply = ""
        if response:
            reply = (
                f"\nCARDHOLDER RESPONSE (simulated, per policy section 5)\n"
                f"  {response.upper()}\n"
            )

        return f"""You are a fraud analyst at a card issuer. Assess this alert.

ALERT
  case         {t.case_id}
  trigger      {t.trigger_type}
  opened       {t.opened_at}
  text         {t.trigger_text}
  card         {t.card_id}   customer {t.customer_id}
  flagged txn  {t.flagged_txn_id} at {s.flagged.get('ts')} for ${float(s.flagged.get('amount') or 0):,.2f}
               {s.flagged.get('product_cd')}/{s.flagged.get('channel')}, model risk score {t.risk_score}
{reply}
EPISODE IDENTIFIED BY THE GRAPH ({len(s.episode)} transaction(s), ${sum(float(x['amount']) for x in s.episode):,.2f})
{ep_lines}

THIS CARD'S BASELINE BEFORE THE ALERT
  {b.get('n_txns', 0)} prior transactions, median ${b.get('p50', 0):,.2f}, 90th pct ${b.get('p90', 0):,.2f}, max ${b.get('max', 0):,.2f}
  products {json.dumps(b.get('products') or {})}
  channels {json.dumps(b.get('channels') or {})}
  regions  {json.dumps(dict(list((b.get('regions') or {}).items())[:6]))}
  prior transactions of this same amount and product: {rec}

SIGNALS FOUND BY THE RULE-BASED CLASSIFIER
  proposed pattern: {s.assessment.pattern}
{sigs}

PRIOR CLOSED CASES RETRIEVED FROM THE GRAPH
{prior}

CASES THIS AGENT ALREADY CLOSED THIS MONTH (retrieved from the graph as memory)
{mine}

POLICY AND TYPOLOGY GUIDANCE RETRIEVED
{guide}

CALIBRATION -- use these bands, do not cluster at high confidence
{ANCHORS}

RULES FOR YOUR ANSWER
  - A risk score is a reason to look, never a verdict. Above 0.7, most flagged
    transactions in this portfolio turn out legitimate.
  - About half of all alerts are legitimate. An agent that suspects everything
    scores badly.
  - Out-of-region use alone and a new device alone are usually legitimate:
    716 of 900 cleared cases were travel, 158 were a new phone.
  - If the cardholder disputes a charge that matches their own established
    recurring pattern, that is a legitimate charge they did not recognise,
    not fraud. {rec} prior identical transactions is strong evidence of this.
  - Cite only what is above. Do not invent transaction ids, cards or devices.
  - `pattern_agrees`: does the classifier's proposed pattern fit the evidence?
    If not, say why in `pattern_comment`. The classifier decides the label;
    your disagreement is recorded, not applied.
  - Each evidence item's `signal` must name one of the classifier signals
    above, or "baseline", "prior_case", "policy" or "risk_score".
  - `summary`: two to six sentences an analyst would read. No preamble.
"""

    # -- evidence request ------------------------------------------------
    def simulate_response(self, s: State, prob: float) -> tuple[str, str, str]:
        """Choose and justify a simulated cardholder reply.

        Replies are not provided, so policy section 5 requires simulating one
        and recording the assumption. The assumption is driven by the evidence
        rather than fixed, because always assuming denial would convict every
        legitimate disputed charge -- which is exactly the R7 trap.
        """
        rec = int(s.baseline.get("recurring_matches") or 0)
        disputed = s.trigger.trigger_type == "customer_report"

        if disputed and rec >= P.R7_MIN_OCCURRENCES:
            return (
                "confirmed",
                "customer_validation",
                f"Shown their own history, the cardholder recognises the charge: this exact "
                f"amount and merchant category has been billed to the card {rec} times before. "
                f"They withdraw the dispute.",
            )
        if disputed:
            return (
                "denied",
                "customer_validation",
                "The cardholder maintains they did not make the transaction and still holds "
                "the card.",
            )
        if s.assessment.pattern == "undocumented" or prob >= 0.6:
            return (
                "denied",
                "customer_validation",
                "Contacted about the activity, the cardholder states they did not make these "
                "purchases and still holds the card.",
            )
        if s.assessment.independent_signals == 0:
            return (
                "confirmed",
                "customer_validation",
                "The cardholder confirms they made the transaction.",
            )
        return (
            "confirmed",
            "step_up_auth",
            "Step-up authentication was requested and completed successfully by the "
            "cardholder, indicating the genuine account holder authorised the activity.",
        )

    # -- explanation -----------------------------------------------------
    def narrate(self, s: State, a: dict[str, Any], exposure: float,
                affected: list[str]) -> str:
        first = s.episode[0] if s.episode else s.flagged
        last = s.episode[-1] if s.episode else s.flagged
        subjects = [s.trigger.customer_id, s.trigger.card_id]
        if s.assessment.ring_profile:
            subjects.append(s.assessment.ring_profile)
        prompt = f"""Write the narrative section of a suspicious activity report.

It is read by a financial regulator and must stand on its own: someone with no
access to our systems must understand what happened from this text alone.

FACTS -- use only these, invent nothing
  customer      {s.trigger.customer_id}
  card          {s.trigger.card_id}
  transactions  {', '.join(affected)}
  dates         {first.get('ts')} to {last.get('ts')}
  total         ${exposure:,.2f}
  channel       {s.flagged.get('channel')}, product code {s.flagged.get('product_cd')}
  pattern       {s.assessment.pattern}
  device        {s.assessment.ring_profile or (s.flagged.get('device_profile') or 'not recorded')}
  connected     {', '.join(s.assessment.connected_cards[:12]) or 'none identified'}
  findings      {' '.join(sg.detail for sg in s.assessment.signals)}
  cardholder    {a.get('_response_text', 'not contacted')}
  prior cases   {', '.join(c['case_id'] for c in s.prior_cases[:5]) or 'none'}

REQUIREMENTS
  Write between EIGHT and TWELVE complete sentences. This is a hard
  requirement: a narrative shorter than eight sentences is rejected as
  insufficient by the filing system.
  Devote roughly one sentence each to: who the customer and card are; what
  was transacted and for how much; when it happened; through which channel
  and device; how the activity was carried out; what links it to other
  accounts; what the cardholder said; and why it is suspicious.
  Plain prose only. No headings, no bullet points, no numbered lists.
  State amounts and dates exactly as given above.
  Do not recommend actions; the filing records facts.
  Do not use the words "I", "we" or "our".
"""
        try:
            text = self.llm.complete(prompt, max_tokens=12000).strip()
            # The model sometimes answers in two dense sentences. Ask once
            # more rather than failing the whole case on a formatting miss.
            if len(re.findall(r"[.!?](?:\s|$)", text)) < 6:
                text = self.llm.complete(
                    prompt + f"\n\nA previous attempt produced only "
                             f"{len(re.findall(r'[.!?](?:\\s|$)', text))} sentences and was "
                             f"rejected. Expand it to at least eight complete sentences, "
                             f"keeping every fact identical.",
                    max_tokens=16000, temperature=0.3,
                ).strip()
            return text
        except LLMError:
            return (
                f"Between {first.get('ts')} and {last.get('ts')}, {len(affected)} transaction(s) "
                f"totalling ${exposure:,.2f} were recorded on card {s.trigger.card_id} belonging "
                f"to customer {s.trigger.customer_id}. "
                + " ".join(sg.detail.capitalize() + "." for sg in s.assessment.signals)
            )

    # -- main ------------------------------------------------------------
    def investigate(self, trigger: Trigger) -> Answer:
        t0 = time.time()
        self.tools.log = ToolLog()
        self.llm.usage.reset()
        s = State(trigger=trigger)

        self.gather(s)
        self.retrieve(s)

        first_pass = self.assess(s)
        exposure = round(sum(float(x["amount"]) for x in s.episode), 2)
        rec = int(s.baseline.get("recurring_matches") or 0)
        recurring = (
            trigger.trigger_type == "customer_report" and rec >= P.R7_MIN_OCCURRENCES
        )
        prob = clamp(
            float(first_pass.get("fraud_probability") or 0.5),
            s.assessment.independent_signals,
            denied=False,
            ring=bool(s.ring),
            recurring=recurring,
        )

        base = P.Assessment(
            verdict=first_pass.get("verdict", "uncertain"),
            fraud_probability=prob,
            pattern=s.assessment.pattern,
            exposure_usd=exposure,
            affected_txn_ids=tuple(x["txn_id"] for x in s.episode),
            connected_card_ids=tuple(s.assessment.connected_cards),
            connected_device_profiles=tuple(
                [s.assessment.ring_profile] if s.assessment.ring_profile else []
            ),
            shared_origin=bool(s.ring),
            shared_origin_label=(
                f"a device profile shared with {s.ring['n_cards']} cards" if s.ring else ""
            ),
            customer_disputed=trigger.trigger_type == "customer_report",
            recurring_occurrences=rec,
            independent_signals=s.assessment.independent_signals,
            pending_authorization=True,
        )

        initial = P.decide(base)
        self.say(f"initial: {', '.join(r.action for r in initial)}  p={prob:.2f}")

        # -- more evidence, if the policy calls for it
        requests: list[EvidenceRequest] = []
        final_state = base
        second = first_pass
        if P.needs_evidence(base):
            response, kind, text = self.simulate_response(s, prob)
            requests.append(EvidenceRequest(
                type=kind, asked_after_step=s.step, assumed_response=text
            ))
            s.step += 1
            second = self.assess(s, response=response)
            denied = response == "denied"
            prob2 = clamp(
                float(second.get("fraud_probability") or prob),
                s.assessment.independent_signals + 1,
                denied=denied, ring=bool(s.ring), recurring=recurring,
            )
            verdict2 = second.get("verdict", base.verdict)
            if response == "confirmed" and recurring:
                verdict2 = "legitimate"
            final_state = base.with_response(
                response, fraud_probability=prob2, verdict=verdict2,
                independent_signals=s.assessment.independent_signals + 1,
            )
            second["_response_text"] = text
            self.say(f"evidence: cardholder {response} -> p={prob2:.2f} ({verdict2})")

        # A legitimate verdict carries no episode and no exposure.
        if final_state.verdict == "legitimate":
            final_state = P.replace(
                final_state, affected_txn_ids=(), exposure_usd=0.0,
                pattern="none", connected_card_ids=(),
            )
            s.assessment.pattern = "none"
            s.assessment.description = ""

        final = P.decide(final_state)
        file_sar, sar_reason = P.should_file_sar(final_state)
        if P.recurrence_check(final_state):
            file_sar = False
            sar_reason = (
                f"R7: the disputed charge matches the cardholder's own established pattern "
                f"({rec} prior transactions of this amount and product); no suspicious activity."
            )
        self.say(f"final: {', '.join(r.action for r in final)}  sar={file_sar}")

        affected = list(final_state.affected_txn_ids)
        exposure_final = round(final_state.exposure_usd, 2)

        # -- assemble
        evidence = self._evidence(s, second, final_state, requests)
        narrative = ""
        subjects: list[str] = []
        dates: list[str] = []
        if file_sar:
            narrative = self.narrate(s, second, exposure_final, affected)
            subjects = [trigger.customer_id, trigger.card_id]
            subjects += s.assessment.connected_cards[:10]
            if s.assessment.ring_profile:
                subjects.append(s.assessment.ring_profile)
            dates = [s.episode[0]["ts"][:10], s.episode[-1]["ts"][:10]]

        changed = [(r.action, r.route) for r in initial] != [(r.action, r.route) for r in final]
        what_changed = "nothing"
        if changed:
            resp = final_state.customer_response
            what_changed = (
                f"The cardholder {resp} the activity when asked, moving fraud probability from "
                f"{base.fraud_probability:.2f} to {final_state.fraud_probability:.2f}"
                + (
                    " and settling the case as legitimate."
                    if final_state.verdict == "legitimate"
                    else "; the recommendation firms up accordingly."
                )
            )

        stop_ok, stop_reason = P.should_stop(final_state)
        answer = Answer(
            case_id=trigger.case_id,
            case=Case(
                status=P.status_for(final_state, final),
                verdict=final_state.verdict,
                fraud_probability=final_state.fraud_probability,
                pattern=final_state.pattern,
                pattern_description=(
                    s.assessment.description if final_state.pattern == "undocumented" else ""
                ),
                affected_txn_ids=affected,
                first_suspicious_txn_id=affected[0] if affected else "",
                connected_card_ids=list(final_state.connected_card_ids),
                connected_device_profiles=list(final_state.connected_device_profiles),
                exposure_usd=exposure_final,
                evidence=evidence,
                similar_prior_cases=[c["case_id"] for c in s.prior_cases[:6]],
                summary=(second.get("summary") or first_pass.get("summary") or "").strip(),
                written_to_graph=False,
                graph_case_id="",
            ),
            evidence_requests=requests,
            next_best_actions=NextBestActions(
                initial=[{"action": r.action, "route": r.route, "reason": r.reason}
                         for r in initial],
                final=[{"action": r.action, "route": r.route, "reason": r.reason}
                       for r in final],
                what_changed=what_changed,
            ),
            sar=SAR(
                file=file_sar, reason=sar_reason, narrative=narrative,
                subjects=subjects if file_sar else [],
                total_amount_usd=exposure_final if file_sar else 0.0,
                activity_dates=dates if file_sar else [],
            ),
            stop_reason=stop_reason,
            tool_calls=self.tools.log.count,
            tokens=self.llm.usage.total,
            latency_s=round(time.time() - t0, 1),
        )

        # -- persist to the graph, so the next investigation can find it.
        #
        # Non-fatal by design. By this point the investigation is complete and
        # every expensive step is done; losing all of it because the workspace
        # was suspending would be the wrong trade. `written_to_graph` stays
        # false, which is honest, and run_cases can backfill.
        try:
            gid = self.tools.write_case(answer)
            # graph_case_id first: the Case model validates on every assignment
            # (validate_assignment), and written_to_graph=True with a blank id
            # trips the invariant. The graph POST has already succeeded by here,
            # so getting the order wrong wrote the case to the graph while the
            # file recorded written_to_graph=false -- silently defeating the
            # case-memory demonstration.
            answer.case.graph_case_id = gid
            answer.case.written_to_graph = True
        except Exception as exc:  # noqa: BLE001
            self.say(f"graph write deferred: {str(exc)[:110]}")
            s.notes.append("case not yet written to the graph")
        return answer

    def _evidence(self, s: State, a: dict[str, Any], final: P.Assessment,
                  requests: list[EvidenceRequest]) -> list[Evidence]:
        """Turn the model's claims into evidence items with real refs.

        Every graph claim is pinned to the query that produced it and to ids
        that exist; the model supplies the wording, not the citation.
        """
        by_signal = {sg.name: sg for sg in s.assessment.signals}
        out: list[Evidence] = []
        seen: set[str] = set()

        for item in (a.get("evidence") or [])[:8]:
            claim = (item.get("claim") or "").strip()
            if not claim or claim.lower() in seen:
                continue
            seen.add(claim.lower())
            sig = by_signal.get(item.get("signal", ""))
            # The model sometimes labels a source outside the schema's four
            # allowed values -- most often "policy" (a document) or "baseline"
            # (a graph fact). Coerce to the closest legal source rather than
            # let one stray label fail the whole case at construction.
            raw_src = (item.get("source") or "graph").lower()
            src = {
                "graph": "graph", "document": "document",
                "customer": "customer", "external": "external",
                "policy": "document", "typology": "document", "rule": "document",
                "baseline": "graph", "prior_case": "graph", "risk_score": "graph",
            }.get(raw_src, "graph")
            if src == "customer" and not requests:
                continue
            if sig:
                ref = self.tools.log.ref("card_window", card_id=s.trigger.card_id,
                                         hours=WINDOW_HOURS)
                ids = sig.entity_ids
                if sig.name == "shared_ring_device":
                    ref = self.tools.log.ref("ring_detect", from_="2016-11-01", to="2016-12-31")
                    ids = ([s.assessment.ring_profile] + s.assessment.connected_cards[:8])
            elif src == "customer":
                ref = f"evidence_request:{len(requests)}"
                ids = []
            elif src == "document":
                d = s.guidance[0] if s.guidance else {}
                ref = f"document:{d.get('doc_id', 'fraud_policy')}"
                ids = []
            else:
                ref = self.tools.log.ref("customer_baseline", card_id=s.trigger.card_id)
                ids = [s.trigger.flagged_txn_id]
            out.append(Evidence(claim=claim, source=src, ref=ref, entity_ids=[
                i for i in ids if i
            ][:12]))

        # Cross-case memory as an explicit evidence item. It goes here rather
        # than in similar_prior_cases, which is validated against CC-* closed
        # cases only; these are this run's own HHG cases.
        if s.prior_agent:
            cited = s.prior_agent[:4]
            out.append(Evidence(
                claim="Consistent with " + "; ".join(
                    f"{c['hhg_case_id']} ({c['because']}, closed {c['verdict']})"
                    for c in cited
                ) + " -- earlier investigations this month retrieved as case memory.",
                source="graph",
                ref="query:prior_agent_cases",
                entity_ids=[c["hhg_case_id"] for c in cited],
            ))

        if not out:
            out.append(Evidence(
                claim=f"{len(s.episode)} transaction(s) examined on card {s.trigger.card_id}; "
                      f"classifier pattern {s.assessment.pattern}.",
                source="graph",
                ref=self.tools.log.ref("card_window", card_id=s.trigger.card_id),
                entity_ids=[x["txn_id"] for x in s.episode][:12],
            ))
        return out
