"""Load the narrowed CSVs into FraudGraph.

Uses the REST++ upsert API in batches rather than a GSQL loading job. The
loading job is faster for a one-shot bulk load, but it needs the file on the
server, and Savanna free-tier workspaces auto-suspend. Batched upserts are
resumable: if the workspace suspends halfway through, rerunning picks up at
the last completed stage instead of starting over. The same code runs
unchanged against Community Edition.

    python src/load_graph.py                 # resume from checkpoint
    python src/load_graph.py --restart       # ignore the checkpoint
    python src/load_graph.py --only devices  # run a single stage
    python src/load_graph.py --verify        # counts only, load nothing
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_client import TigerGraph  # noqa: E402

csv.field_size_limit(10_000_000)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
CHECKPOINT = DATA / ".load_checkpoint.json"

BATCH = 4_000          # vertices or edges per HTTP round trip
STAGES = ("customers", "cards", "transactions", "devices", "closed_cases", "next_chain")


def blank(v: str | None) -> str:
    v = (v or "").strip()
    return "" if v.lower() in ("", "nan", "none", "null") else v


def num(v: str | None) -> float:
    try:
        return float(blank(v) or 0)
    except ValueError:
        return 0.0


class Loader:
    def __init__(self, tg: TigerGraph, dry_run: bool = False) -> None:
        self.tg = tg
        self.dry_run = dry_run
        self.sent = 0

    @staticmethod
    def _wrap(vertices: dict) -> dict:
        """REST++ wants {"attr": {"value": v}}, not {"attr": v}.

        Passing bare values is not an error: TigerGraph creates the vertex and
        silently leaves every attribute at its default. The first load of this
        graph produced 590,742 transactions whose ts was all 1970-01-01 and
        whose amount was all zero, and the vertex counts matched perfectly.
        Only querying the attributes revealed it.
        """
        return {
            vtype: {
                vid: {k: {"value": v} for k, v in attrs.items()}
                for vid, attrs in rows.items()
            }
            for vtype, rows in vertices.items()
        }

    def upsert(self, vertices: dict | None = None, edges: dict | None = None) -> None:
        payload: dict[str, Any] = {}
        if vertices:
            payload["vertices"] = self._wrap(vertices)
        if edges:
            payload["edges"] = edges
        if not payload or self.dry_run:
            return
        for attempt in range(4):
            try:
                self.tg.rest(
                    "POST", f"/graph/{self.tg.graph}", json=payload, timeout=300
                )
                self.sent += 1
                return
            except Exception as exc:  # noqa: BLE001
                # Savanna can be waking from auto-suspend; back off and retry.
                if attempt == 3:
                    raise
                wait = 5 * (attempt + 1)
                print(f"\n    upsert failed ({str(exc)[:90]}), retrying in {wait}s", flush=True)
                time.sleep(wait)


def batched(it: Iterator[tuple[str, dict]], size: int = BATCH) -> Iterator[dict]:
    chunk: dict[str, dict] = {}
    for key, attrs in it:
        chunk[key] = attrs
        if len(chunk) >= size:
            yield chunk
            chunk = {}
    if chunk:
        yield chunk


def progress(label: str, n: int, total: int | None, t0: float) -> None:
    el = time.time() - t0
    pct = f"{100 * n / total:5.1f}%" if total else "     "
    print(f"\r  {label:<14} {n:>9,} {pct}  {el:6.1f}s", end="", flush=True)


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------

def stage_customers(L: Loader) -> None:
    t0 = time.time()
    cards_per: dict[str, int] = defaultdict(int)
    with open(DATA / "txn_narrow.csv", encoding="utf-8", newline="") as fh:
        seen: set[tuple[str, str]] = set()
        for r in csv.DictReader(fh):
            key = (r["customer_id"], r["card_id"])
            if key not in seen:
                seen.add(key)
                cards_per[r["customer_id"]] += 1

    n = 0
    for chunk in batched(iter((c, {"n_cards": k}) for c, k in cards_per.items())):
        L.upsert(vertices={"Customer": chunk})
        n += len(chunk)
        progress("customers", n, len(cards_per), t0)
    print()


def stage_cards(L: Loader) -> None:
    t0 = time.time()
    cards: dict[str, dict] = {}
    owns: dict[str, dict] = defaultdict(dict)
    with open(DATA / "txn_narrow.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            cid = r["card_id"]
            if cid and cid not in cards:
                cards[cid] = {
                    "customer_id": r["customer_id"],
                    "issuer": blank(r["card1"]),
                    "network": blank(r["card4"]),
                    "card_type": blank(r["card6"]),
                }
                owns[r["customer_id"]][cid] = {}

    n = 0
    for chunk in batched(iter(cards.items())):
        L.upsert(vertices={"Card": chunk})
        n += len(chunk)
        progress("cards", n, len(cards), t0)
    print()

    n = 0
    items = [(cust, {"OWNS": {"Card": targets}}) for cust, targets in owns.items()]
    for chunk in batched(iter(items), 2_000):
        L.upsert(edges={"Customer": chunk})
        n += len(chunk)
        progress("OWNS", n, len(items), t0)
    print()


def stage_transactions(L: Loader) -> dict[str, list[tuple[str, str]]]:
    """Transaction vertices plus MADE, BILLED_IN and PURCHASER_EMAIL.

    Returns the per-card ordering needed to build the NEXT chain, so the
    590k-row file is only read once.
    """
    t0 = time.time()
    order: dict[str, list[tuple[str, str]]] = defaultdict(list)

    vbuf: dict[str, dict] = {}
    made: dict[str, dict] = defaultdict(dict)
    billed: dict[str, dict] = {}
    email: dict[str, dict] = {}
    regions: dict[str, dict] = {}
    domains: dict[str, dict] = {}
    n = 0

    def flush() -> None:
        nonlocal vbuf, made, billed, email, regions, domains
        if regions:
            L.upsert(vertices={"BillingRegion": regions})
        if domains:
            L.upsert(vertices={"EmailDomain": domains})
        if vbuf:
            L.upsert(vertices={"Transaction": vbuf})
        e: dict[str, Any] = {}
        if made:
            e["Card"] = {c: {"MADE": {"Transaction": t}} for c, t in made.items()}
        if billed or email:
            tx: dict[str, dict] = defaultdict(dict)
            for t, r in billed.items():
                tx[t]["BILLED_IN"] = {"BillingRegion": r}
            for t, d in email.items():
                tx[t]["PURCHASER_EMAIL"] = {"EmailDomain": d}
            e["Transaction"] = tx
        if e:
            L.upsert(edges=e)
        vbuf, made, billed, email = {}, defaultdict(dict), {}, {}
        regions, domains = {}, {}

    with open(DATA / "txn_narrow.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            tid, cid = r["TransactionID"], r["card_id"]
            vbuf[tid] = {
                "ts": r["ts"],
                "amount": num(r["TransactionAmt"]),
                "product_cd": blank(r["ProductCD"]),
                "channel": blank(r["channel"]),
                "risk_score": num(r["risk_score"]),
                "addr1": blank(r["addr1"]),
                "addr2": blank(r["addr2"]),
                "dist1": blank(r["dist1"]),
                "dist2": blank(r["dist2"]),
                "p_email": blank(r["P_emaildomain"]),
                "r_email": blank(r["R_emaildomain"]),
                "card_id": cid,
                "customer_id": r["customer_id"],
                **{f"M{i}": blank(r.get(f"M{i}")) for i in range(1, 10)},
            }
            if cid:
                made[cid][tid] = {}
                order[cid].append((r["ts"], tid))
            if a1 := blank(r["addr1"]):
                regions[a1] = {}
                billed[tid] = {a1: {}}
            if dom := blank(r["P_emaildomain"]):
                domains[dom] = {}
                email[tid] = {dom: {}}

            n += 1
            if len(vbuf) >= BATCH:
                flush()
                progress("transactions", n, 590_742, t0)
    flush()
    progress("transactions", n, 590_742, t0)
    print()
    return order


def stage_devices(L: Loader) -> None:
    t0 = time.time()
    profiles: dict[str, dict] = {}
    with open(DATA / "device_profiles.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            profiles[r["device_profile"]] = {
                "device_info": r["device_info"],
                "os": r["os"],
                "browser": r["browser"],
                "screen": r["screen"],
                "dominant_proxy": r["dominant_proxy"],
                "n_records": int(r["n_records"]),
            }
    n = 0
    for chunk in batched(iter(profiles.items())):
        L.upsert(vertices={"DeviceProfile": chunk})
        n += len(chunk)
        progress("devices", n, len(profiles), t0)
    print()

    # FROM_DEVICE edges, plus the identity signals copied onto the transaction.
    #
    # device_new (id_15) and device_proxy (id_23) live on Transaction rather
    # than on DeviceProfile because ring detection needs them *per window*: a
    # profile's new-to-account ratio over November is the signal, not its
    # lifetime average. Aggregating them in GSQL requires them on the
    # transactions being aggregated.
    n = 0
    ebuf: dict[str, dict] = {}
    vbuf: dict[str, dict] = {}
    with open(DATA / "identity_narrow.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            tid, prof = r["TransactionID"], r["device_profile"]
            ebuf[tid] = {"FROM_DEVICE": {"DeviceProfile": {prof: {}}}}
            vbuf[tid] = {
                "device_profile": prof,
                "device_new": blank(r.get("id_15")),
                "device_proxy": blank(r.get("id_23")),
            }
            if len(ebuf) >= BATCH:
                L.upsert(vertices={"Transaction": vbuf}, edges={"Transaction": ebuf})
                n += len(ebuf)
                ebuf, vbuf = {}, {}
                progress("FROM_DEVICE", n, 144_432, t0)
    if ebuf:
        L.upsert(vertices={"Transaction": vbuf}, edges={"Transaction": ebuf})
        n += len(ebuf)
    progress("FROM_DEVICE", n, 144_432, t0)
    print()


def stage_closed_cases(L: Loader) -> None:
    t0 = time.time()
    verts: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    with open(RAW / "closed_cases_history.csv", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            cid = r["case_id"]
            verts[cid] = {
                "customer_id": r["customer_id"],
                "card_id": r["card_id"],
                "opened_at": r["opened_at"],
                "closed_at": r["closed_at"],
                "outcome": r["outcome"],
                "pattern": r["pattern"],
                "first_fraud_txn_id": blank(r["first_fraud_txn_id"]),
                "n_txns": int(r["n_txns"] or 0),
                "exposure_usd": num(r["exposure_usd"]),
                "actions_taken": r["actions_taken"],
                "report_filed": r["report_filed"].strip().lower() in ("yes", "true", "1"),
                "analyst_notes": r["analyst_notes"],
            }
            out: dict[str, Any] = {"ON_CARD": {"Card": {r["card_id"]: {}}}}
            if txns := [t for t in r["txn_ids"].split("|") if t]:
                out["INVOLVES"] = {"Transaction": {t: {} for t in txns}}
            if conn := [c for c in r["connected_card_ids"].split("|") if c]:
                out["CONNECTED_TO"] = {"Card": {c: {} for c in conn}}
            edges[cid] = out

    for chunk in batched(iter(verts.items()), 1_000):
        L.upsert(vertices={"ClosedCase": chunk})
    progress("closed cases", len(verts), len(verts), t0)
    print()
    for chunk in batched(iter(edges.items()), 1_000):
        L.upsert(edges={"ClosedCase": chunk})
    progress("case edges", len(edges), len(edges), t0)
    print()


def stage_next_chain(L: Loader, order: dict[str, list[tuple[str, str]]]) -> None:
    """Link each card's transactions in time order.

    Walking this backwards is how the agent finds where an episode began --
    the flagged transaction is often not the first one.
    """
    t0 = time.time()
    total = sum(max(0, len(v) - 1) for v in order.values())
    buf: dict[str, dict] = {}
    n = 0
    for _card, rows in order.items():
        rows.sort()
        for (_, a), (_, b) in zip(rows, rows[1:]):
            buf[a] = {"NEXT": {"Transaction": {b: {}}}}
            if len(buf) >= BATCH:
                L.upsert(edges={"Transaction": buf})
                n += len(buf)
                buf = {}
                progress("NEXT", n, total, t0)
    if buf:
        L.upsert(edges={"Transaction": buf})
        n += len(buf)
    progress("NEXT", n, total, t0)
    print()


# --------------------------------------------------------------------------

def read_checkpoint() -> set[str]:
    if CHECKPOINT.exists():
        return set(json.loads(CHECKPOINT.read_text()).get("done", []))
    return set()


def write_checkpoint(done: set[str]) -> None:
    CHECKPOINT.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")


EXPECTED = {
    "Customer": 13_553,
    "Card": 14_318,
    "Transaction": 590_742,
    "DeviceProfile": 9_706,
    "ClosedCase": 5_565,
}


def count(tg: TigerGraph, vtype: str) -> int | str:
    try:
        out = tg.rest("GET", f"/graph/{tg.graph}/vertices/{vtype}?count_only=true")
        res = out.get("results")
        if isinstance(res, list) and res:
            return int(res[0].get("count", 0))
        return 0
    except Exception as exc:  # noqa: BLE001
        return f"? ({str(exc)[:50]})"


def verify(tg: TigerGraph) -> None:
    print("\nvertex counts:")
    ok = True
    for vt in ("Customer", "Card", "Transaction", "DeviceProfile",
               "EmailDomain", "BillingRegion", "ClosedCase", "AgentCase"):
        n = count(tg, vt)
        want = EXPECTED.get(vt)
        if isinstance(n, int):
            flag = ""
            if want is not None:
                if n == want:
                    flag = "  ok"
                else:
                    flag = f"  EXPECTED {want:,}"
                    ok = False
            print(f"  {vt:<15} {n:>12,}{flag}")
        else:
            print(f"  {vt:<15} {n}")
            ok = False
    if not ok:
        print("\n  Counts differ from the source files. Rerun the affected stage.")

    # Counts alone are not evidence the load worked -- they were all correct
    # while every attribute was empty. Spot-check real values.
    print("\nattribute spot-check:")
    probes = [
        ("Transaction", "3476682", "ts", "2016-11-21 20:30:00"),
        ("Transaction", "3476682", "amount", 482.12),
        ("Transaction", "3476682", "card_id", "C07297-K1"),
        ("Card", "C07297-K1", "customer_id", "C07297"),
        ("ClosedCase", "CC-0001", "outcome", "confirmed_fraud"),
    ]
    for vtype, vid, attr, want in probes:
        try:
            out = tg.rest(
                "GET", f"/graph/{tg.graph}/vertices/{vtype}/{quote(vid, safe='')}"
            )
            res = out.get("results") or []
            got = res[0].get("attributes", {}).get(attr) if res else None
        except Exception as exc:  # noqa: BLE001
            got = f"error: {str(exc)[:40]}"
        good = (
            abs(float(got) - float(want)) < 0.01
            if isinstance(want, float) and isinstance(got, (int, float))
            else got == want
        )
        print(f"  {'ok  ' if good else 'FAIL'} {vtype}.{attr} = {got!r}"
              + ("" if good else f"  expected {want!r}"))
        if not good:
            ok = False

    if not ok:
        print("\n  Load is incomplete. Rerun: python src/load_graph.py --restart")
    else:
        print("\n  Graph loaded and verified.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--only", choices=STAGES)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tg = TigerGraph()
    print(f"host  : {tg.host}")
    print(f"graph : {tg.graph}")
    if not tg.graph_exists():
        print(f"\nGraph {tg.graph} does not exist. Run: python src/create_schema.py")
        return 1

    if args.verify:
        verify(tg)
        return 0

    done = set() if args.restart else read_checkpoint()
    if args.only:
        done = set(STAGES) - {args.only}
    if done:
        print(f"resuming, already done: {sorted(done)}")
    print()

    L = Loader(tg, dry_run=args.dry_run)
    order: dict[str, list[tuple[str, str]]] = {}
    t0 = time.time()

    if "customers" not in done:
        stage_customers(L)
        done.add("customers"); write_checkpoint(done)
    if "cards" not in done:
        stage_cards(L)
        done.add("cards"); write_checkpoint(done)
    if "transactions" not in done or "next_chain" not in done:
        order = stage_transactions(L) if "transactions" not in done else {}
        if "transactions" not in done:
            done.add("transactions"); write_checkpoint(done)
    if "devices" not in done:
        stage_devices(L)
        done.add("devices"); write_checkpoint(done)
    if "closed_cases" not in done:
        stage_closed_cases(L)
        done.add("closed_cases"); write_checkpoint(done)
    if "next_chain" not in done:
        if not order:
            print("  (re-reading transactions for the NEXT chain)")
            order = defaultdict(list)
            with open(DATA / "txn_narrow.csv", encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    if r["card_id"]:
                        order[r["card_id"]].append((r["ts"], r["TransactionID"]))
        stage_next_chain(L, order)
        done.add("next_chain"); write_checkpoint(done)

    print(f"\nloaded in {time.time() - t0:,.0f}s, {L.sent:,} upsert calls")
    verify(tg)
    print("\nNext: python src/create_queries.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
