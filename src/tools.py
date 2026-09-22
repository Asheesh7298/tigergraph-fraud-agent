"""The agent's tools: GSQL queries wrapped as Python, plus case persistence.

Every graph fact the agent cites passes through here, and every call is
counted -- `tool_calls` in the answer file is a real count, not an estimate.
The same functions are what the MCP server exposes, so the agent and an
external MCP client see exactly the same surface.

    python src/tools.py --smoke     # exercise every tool against the graph
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_client import TigerGraph  # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"


def as_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    v = value.strip().replace("T", " ")
    for fmt in (TS, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp: {value!r}")


def fmt(dt: datetime) -> str:
    return dt.strftime(TS)


def _merge(results: list[dict]) -> dict[str, Any]:
    """GSQL returns a list of single-key PRINT blocks; flatten it."""
    out: dict[str, Any] = {}
    for block in results or []:
        for k, v in block.items():
            out[k] = v
    return out


def agentcase_payload(answer: Any, graph_case_id: str | None = None):
    """Build (vertex_id, attrs, edges) for an AgentCase from an Answer.

    Standalone so both the direct writer and the MCP persist tool build the
    same vertex from the same logic.
    """
    case = answer.case
    cid = graph_case_id or f"CASE-2016-{answer.case_id.split('-')[-1]}"
    attrs = {
        "hhg_case_id": answer.case_id,
        "status": case.status,
        "verdict": case.verdict,
        "pattern": case.pattern,
        "pattern_description": case.pattern_description,
        "fraud_probability": case.fraud_probability,
        "exposure_usd": case.exposure_usd,
        "first_suspicious_txn_id": case.first_suspicious_txn_id,
        "summary": case.summary,
        "stop_reason": answer.stop_reason,
        "sar_filed": answer.sar.file,
        "initial_actions": "|".join(a.action for a in answer.next_best_actions.initial),
        "final_actions": "|".join(a.action for a in answer.next_best_actions.final),
        "written_at": fmt(datetime.now()),
    }
    edges: dict[str, Any] = {}
    if case.affected_txn_ids:
        edges["CASE_INVOLVES"] = {"Transaction": {t: {} for t in case.affected_txn_ids}}
    if case.similar_prior_cases:
        edges["SIMILAR_TO"] = {"ClosedCase": {c: {} for c in case.similar_prior_cases}}
    if case.connected_device_profiles:
        edges["CASE_DEVICE"] = {"DeviceProfile": {d: {} for d in case.connected_device_profiles}}
    return cid, attrs, edges


@dataclass
class ToolLog:
    """Records what the agent actually asked the graph, for the case file."""

    calls: list[dict] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.calls)

    def add(self, name: str, params: dict) -> None:
        self.calls.append({"tool": name, "params": params})

    def ref(self, name: str, **params: Any) -> str:
        """The `ref` string an evidence item cites."""
        inner = ", ".join(f"{k}={v}" for k, v in params.items())
        return f"query:{name}({inner})"


class FraudTools:
    def __init__(self, tg: TigerGraph | None = None, log: ToolLog | None = None) -> None:
        self.tg = tg or TigerGraph()
        self.log = log or ToolLog()

    # -- plumbing --------------------------------------------------------
    def run(self, name: str, **params: Any) -> dict[str, Any]:
        """Invoke an installed query.

        The query string is built by hand with percent-encoding rather than
        handed to requests as a params dict. urlencode renders a space as "+",
        and TigerGraph matches that literally: a device profile key like
        "SM-XXXX Build/YYY | Android 7.0 | ..." then never equals the stored
        attribute and the query returns an empty result rather than an error.

        A JSON POST body also encodes correctly, but TigerGraph will not bind
        VERTEX<> parameters that way, and four of these six take one.
        """
        clean = {k: v for k, v in params.items() if v is not None}
        self.log.add(name, clean)
        qs = "&".join(
            f"{k}={quote(str(v).lower() if isinstance(v, bool) else str(v), safe='')}"
            for k, v in clean.items()
        )
        payload = self.tg.rest("GET", f"/query/{self.tg.graph}/{name}?{qs}")
        return _merge(payload.get("results", []))

    # -- reads -----------------------------------------------------------
    def get_transaction(self, txn_id: str) -> dict[str, Any]:
        """Attributes of one transaction, including its true timestamp.

        Always resolve the flagged transaction through this before windowing:
        a case can be opened hours after the activity it concerns.
        """
        self.log.add("get_transaction", {"txn_id": txn_id})
        out = self.tg.rest(
            "GET", f"/graph/{self.tg.graph}/vertices/Transaction/{quote(txn_id, safe='')}"
        )
        res = out.get("results") or []
        return res[0].get("attributes", {}) if res else {}

    def get_device_profile(self, profile_key: str) -> dict[str, Any]:
        """Device profile keys contain 'Build/NRD90M', whose slash would
        otherwise be read as a path separator and 404."""
        self.log.add("get_device_profile", {"profile_key": profile_key})
        out = self.tg.rest(
            "GET",
            f"/graph/{self.tg.graph}/vertices/DeviceProfile/{quote(profile_key, safe='')}",
        )
        res = out.get("results") or []
        return res[0].get("attributes", {}) if res else {}

    def card_window(
        self, card_id: str, centre: str | datetime, hours: float = 3.0, limit: int = 500
    ) -> dict[str, Any]:
        c = as_dt(centre)
        return self.run(
            "card_window",
            p_card=card_id,
            p_from=fmt(c - timedelta(hours=hours)),
            p_to=fmt(c + timedelta(hours=hours)),
            p_limit=limit,
        )

    def card_window_between(self, card_id: str, start, end, limit: int = 500) -> dict[str, Any]:
        return self.run(
            "card_window", p_card=card_id,
            p_from=fmt(as_dt(start)), p_to=fmt(as_dt(end)), p_limit=limit,
        )

    def customer_baseline(
        self,
        card_id: str,
        before: str | datetime,
        amount: float | None = None,
        product: str | None = None,
        tolerance: float = 2.0,
    ) -> dict[str, Any]:
        out = self.run(
            "customer_baseline",
            p_card=card_id,
            p_before=fmt(as_dt(before)),
            p_amount=amount if amount is not None else -1.0,
            p_product=product or "",
            p_tolerance=tolerance,
        )
        amounts = [float(a) for a in (out.get("amounts") or [])]
        if amounts:
            amounts.sort()
            out["p50"] = round(statistics.median(amounts), 2)
            out["p90"] = round(amounts[min(len(amounts) - 1, int(0.9 * len(amounts)))], 2)
            out["max"] = round(amounts[-1], 2)
            out["mean"] = round(statistics.fmean(amounts), 2)
        else:
            out["p50"] = out["p90"] = out["max"] = out["mean"] = 0.0
        out.pop("amounts", None)   # keep the payload small for the prompt
        return out

    def device_neighbors(self, profile_key: str, start, end) -> dict[str, Any]:
        return self.run(
            "device_neighbors", p_profile=profile_key,
            p_from=fmt(as_dt(start)), p_to=fmt(as_dt(end)),
        )

    def region_history(
        self, card_id: str, region: str, before: str | datetime, parallel_days: int = 3
    ) -> dict[str, Any]:
        return self.run(
            "region_history", p_card=card_id, p_region=region,
            p_before=fmt(as_dt(before)), p_parallel_days=parallel_days,
        )

    def ring_detect(
        self, start, end, min_cards: int = 3, top: int = 25
    ) -> dict[str, Any]:
        """Device profiles behaving like a ring, ranked by concealment.

        Ranking on card count alone surfaces popular browsers, not rings --
        the ring sits at rank 71 by that measure. See ring_detect.gsql.
        """
        out = self.run(
            "ring_detect", p_from=fmt(as_dt(start)), p_to=fmt(as_dt(end)),
            p_min_cards=min_cards, p_top=top,
        )
        cards = out.get("cards_by_profile") or {}
        ring = out.get("ring_score") or {}
        novelty = out.get("novelty_score") or {}
        ranked = [
            {
                "profile": p,
                "n_cards": len(c),
                "ring_score": round(float(ring.get(p, 0)), 2),
                "novelty_score": round(float(novelty.get(p, 0)), 2),
                "new_ratio": round(float((out.get("new_ratio") or {}).get(p, 0)), 3),
                "anon_ratio": round(float((out.get("anon_ratio") or {}).get(p, 0)), 3),
                "n_txns": int((out.get("txns_by_profile") or {}).get(p, 0)),
                "total_usd": round(float((out.get("usd_by_profile") or {}).get(p, 0)), 2),
                "first_seen": (out.get("first_seen") or {}).get(p, ""),
                "last_seen": (out.get("last_seen") or {}).get(p, ""),
                "cards": sorted(c),
            }
            for p, c in cards.items()
        ]
        ranked.sort(key=lambda r: (-r["ring_score"], -r["novelty_score"]))
        out["ranked"] = ranked[:top]
        return out

    def search_documents(
        self, query_vec: list[float], k: int = 6, kind: str = ""
    ) -> list[dict[str, Any]]:
        """Vector search over the policy and typology corpus.

        The query vector is posted as JSON -- a 768-element list will not fit
        in a URL. Vertex parameters are not involved here, so POST is safe.
        """
        self.log.add("search_documents", {"k": k, "kind": kind or "any"})
        payload = self.tg.rest(
            "POST", f"/query/{self.tg.graph}/search_documents",
            json={"p_query": query_vec, "p_k": k, "p_kind": kind},
        )
        return _merge(payload.get("results", [])).get("hits") or []

    def search_closed_cases(
        self, query_vec: list[float], before: str | datetime,
        k: int = 8, outcome: str = "",
    ) -> list[dict[str, Any]]:
        """Vector search over closed-case narratives.

        `before` keeps the replay honest: a case closed after the alert under
        investigation could not have informed it.
        """
        self.log.add("search_closed_cases", {"k": k, "before": str(before)})
        payload = self.tg.rest(
            "POST", f"/query/{self.tg.graph}/search_closed_cases",
            json={"p_query": query_vec, "p_before": fmt(as_dt(before)),
                  "p_k": k, "p_outcome": outcome},
        )
        return _merge(payload.get("results", [])).get("hits") or []

    def similar_closed_cases(
        self, card_id: str, pattern: str = "", exposure: float | None = None, limit: int = 12
    ) -> dict[str, Any]:
        return self.run(
            "similar_closed_cases", p_card=card_id, p_pattern=pattern or "",
            p_exposure=exposure if exposure is not None else -1.0, p_limit=limit,
        )

    # -- writes ----------------------------------------------------------
    def write_case(self, answer: Any, graph_case_id: str | None = None) -> str:
        """Persist the investigation as an AgentCase vertex, with its links.

        This is the case memory the next investigation retrieves -- which is
        why the pack is processed in chronological order.
        """
        cid, attrs, edges = agentcase_payload(answer, graph_case_id)
        self.log.add("write_case", {"graph_case_id": cid})
        self.persist_case(cid, attrs, edges)
        return cid

    def persist_case(self, cid: str, attrs: dict, edges: dict) -> str:
        """Raw AgentCase upsert. Kept separate so the MCP path can call it."""
        payload: dict[str, Any] = {"vertices": {"AgentCase": {cid: attrs}}}
        if edges:
            payload["edges"] = {"AgentCase": {cid: edges}}
        self.tg.rest("POST", f"/graph/{self.tg.graph}", json=payload, timeout=120)
        return cid

    def prior_agent_cases(self, limit: int = 30) -> list[dict[str, Any]]:
        """Cases this agent has already written, for cross-case memory."""
        self.log.add("prior_agent_cases", {"limit": limit})
        out = self.tg.rest(
            "GET", f"/graph/{self.tg.graph}/vertices/AgentCase?limit={limit}"
        )
        return [
            {**r.get("attributes", {}), "__id": r.get("v_id")}
            for r in (out.get("results") or [])
        ]


# --------------------------------------------------------------------------
# Smoke test -- exercises every tool against known facts
# --------------------------------------------------------------------------

def smoke() -> int:
    t = FraudTools()
    ok = True

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  {'OK  ' if cond else 'FAIL'} {label}{(' -- ' + detail) if detail else ''}")
        ok = ok and cond

    print("get_transaction")
    tx = t.get_transaction("3476682")           # HHG-006 flagged leg
    check("HHG-006 flagged txn", tx.get("card_id") == "C07297-K1", str(tx.get("card_id")))
    check("its real ts is the evening before the alert",
          str(tx.get("ts", "")).startswith("2016-11-21 20:30"), str(tx.get("ts")))

    print("\ncard_window  (Ring B burst)")
    w = t.card_window("C07297-K1", tx["ts"], hours=3)
    ids = [r["v_id"] for r in w.get("Txns", [])]
    check("four legs recovered", ids == ["3476602", "3476633", "3476665", "3476682"], str(ids))
    total = sum(r["attributes"]["amount"] for r in w.get("Txns", []))
    check("exposure is $1,906.07", abs(total - 1906.07) < 0.02, f"${total:,.2f}")

    print("\ncustomer_baseline  (R7 recurrence on HHG-018)")
    b = t.customer_baseline("C02354-K2", "2016-11-27 14:41:26", amount=39.08, product="W")
    check("established recurring charge", int(b.get("recurring_matches", 0)) >= 100,
          f"{b.get('recurring_matches')} prior matches")

    print("\ncustomer_baseline  (HHG-010 outlier)")
    b2 = t.customer_baseline("C10434-K1", "2016-12-02 15:18:27")
    check("flagged $1000.03 far exceeds prior max", float(b2.get("max", 0)) < 700,
          f"prior max ${b2.get('max')}")

    print("\ndevice_neighbors  (Ring A)")
    ring_key = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
    d = t.device_neighbors(ring_key, "2016-11-01 00:00:00", "2016-12-31 23:59:59")
    n_cards = len(d.get("cards") or [])
    check("28 cards on the profile", n_cards == 28, f"{n_cards} cards")
    check("60 transactions", int(d.get("n_txns", 0)) == 60, str(d.get("n_txns")))
    check("every one new to its account",
          int(d.get("n_new_to_account", 0)) == int(d.get("n_txns", -1)),
          f"{d.get('n_new_to_account')}/{d.get('n_txns')}")
    check("every one anonymised",
          int(d.get("n_anonymised", 0)) == int(d.get("n_txns", -1)),
          f"{d.get('n_anonymised')}/{d.get('n_txns')}")

    print("\nring_detect  (finds the ring without being told)")
    r = t.ring_detect("2016-11-01 00:00:00", "2016-12-31 23:59:59", min_cards=5)
    top = (r.get("ranked") or [{}])[0]
    check("top-ranked cluster spans 28 cards", top.get("n_cards") == 28,
          f"{top.get('n_cards')} cards, ring_score {top.get('ring_score')}")
    check("it is new to every account it touched", top.get("new_ratio") == 1.0,
          f"new_ratio {top.get('new_ratio')}")
    check("and anonymised on every transaction", top.get("anon_ratio") == 1.0,
          f"anon_ratio {top.get('anon_ratio')}")
    check("HHG-014's card is in it", "C13487-K1" in (top.get("cards") or []),
          f"{len(top.get('cards') or [])} cards listed")
    runner_up = (r.get("ranked") or [{}, {}])[1] if len(r.get("ranked") or []) > 1 else {}
    check("it outranks the next candidate decisively",
          top.get("ring_score", 0) >= 2 * runner_up.get("ring_score", 0.01),
          f"{top.get('ring_score')} vs {runner_up.get('ring_score')}")

    print("\nregion_history  (HHG-001, in-person, region 444)")
    rh = t.region_history("C12382-K1", "444.0", "2016-12-05 01:55:28")
    check("returns a history", "n_total" in rh, json.dumps(rh)[:90])

    print("\nsimilar_closed_cases  (HHG-014's card)")
    s = t.similar_closed_cases("C13487-K1", pattern="undocumented")
    check("retrieval ran", "retrieved_because" in s, str(list(s))[:90])

    print(f"\n{t.log.count} tool calls made")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        return smoke()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
