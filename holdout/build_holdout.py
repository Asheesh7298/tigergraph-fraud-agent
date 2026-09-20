"""Build a holdout set from October closed cases, and keep the truth aside.

The twenty exam cases are scored against an answer key we cannot see, so the
only real feedback signal available is the bank's own closed history. October
is the last month of it: cases from then are recent enough to resemble the
November-December exam period, and every one carries a confirmed outcome.

Each case is stripped back to what an alert would have looked like at the
moment it opened -- a trigger, a flagged transaction, a card -- and the
outcome, pattern, transaction list, exposure, report decision and connected
cards are withheld for scoring.

    python holdout/build_holdout.py --n 30

Caveat worth stating in the repo and in the blog: these narratives are
templated, so an agent can do better here than on the exam by learning the
template rather than the fraud. The number is optimistic. What it catches
reliably is direction of error -- over-blocking, over-reporting, systematic
miscalibration -- and that is what it is for.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

csv.field_size_limit(10_000_000)

RAW = ROOT / "data" / "raw"
OUT = ROOT / "holdout"

#: Mirror the exam pack's trigger mix: 11 risk_score, 8 customer_report,
#: 1 analyst_request out of 20.
TRIGGER_MIX = [("risk_score", 0.55), ("customer_report", 0.40), ("analyst_request", 0.05)]

PACK_FIELDS = [
    "case_id", "opened_at", "trigger_type", "trigger_text",
    "flagged_txn_id", "card_id", "customer_id", "risk_score",
]
TRUTH_FIELDS = [
    "case_id", "outcome", "pattern", "first_fraud_txn_id", "txn_ids", "n_txns",
    "exposure_usd", "connected_card_ids", "actions_taken", "report_filed",
]


def load_amounts(txn_ids: set[str]) -> dict[str, dict[str, str]]:
    narrow = ROOT / "data" / "txn_narrow.csv"
    if not narrow.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    with open(narrow, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["TransactionID"] in txn_ids:
                out[r["TransactionID"]] = r
    return out


def pick_trigger(rng: random.Random) -> str:
    x = rng.random()
    acc = 0.0
    for name, share in TRIGGER_MIX:
        acc += share
        if x <= acc:
            return name
    return "risk_score"


def trigger_text(kind: str, case: dict[str, str], txn: dict[str, str]) -> str:
    tid = case["_flagged"]
    amt = float(txn.get("TransactionAmt") or 0)
    cust = case["customer_id"]
    if kind == "customer_report":
        return (
            f"Customer {cust} message: 'I never made this ${amt:,.2f} purchase. "
            f"Please check my card.' Refers to {tid}."
        )
    if kind == "analyst_request":
        return (
            f"Analyst request: review transaction {tid} on card {case['card_id']} "
            f"and look for related activity."
        )
    where = (
        f"in billing region {txn.get('addr1')}"
        if txn.get("channel") == "in_person" else "online"
    )
    return (
        f"Real-time model scored transaction {tid} (${amt:,.2f}, {where}) at "
        f"{float(txn.get('risk_score') or 0):.2f}. Review and decide."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--month", default="2016-10")
    ap.add_argument("--seed", type=int, default=20260919)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    rows = []
    for r in csv.DictReader(open(RAW / "closed_cases_history.csv", encoding="utf-8")):
        if not r["opened_at"].startswith(args.month):
            continue
        # Cleared cases have no first_fraud_txn_id -- there was no fraud. The
        # transaction the model flagged is in txn_ids. Requiring
        # first_fraud_txn_id silently drops every legitimate case, leaving a
        # holdout that cannot detect over-blocking, which is most of what it
        # is for.
        flagged = r["first_fraud_txn_id"].strip() or r["txn_ids"].split("|")[0].strip()
        if flagged:
            rows.append({**r, "_flagged": flagged})
    print(f"{len(rows):,} closed cases opened in {args.month}")

    # Sample in proportion to the real outcome mix so the holdout is not
    # accidentally all fraud -- the history is 84% confirmed fraud, but the
    # exam is described as about half legitimate, so both are represented
    # rather than letting one dominate.
    fraud = [r for r in rows if r["outcome"] == "confirmed_fraud"]
    cleared = [r for r in rows if r["outcome"] == "cleared"]
    n_cleared = max(1, round(args.n * 0.40))
    n_fraud = args.n - n_cleared
    rng.shuffle(fraud)
    rng.shuffle(cleared)
    picked = fraud[:n_fraud] + cleared[:n_cleared]
    rng.shuffle(picked)
    print(f"sampled {len(picked)}: {n_fraud} confirmed fraud, "
          f"{min(n_cleared, len(cleared))} cleared")

    txns = load_amounts({r["_flagged"] for r in picked})
    if not txns:
        print("warning: data/txn_narrow.csv not found; trigger text will lack amounts")

    pack: list[dict[str, str]] = []
    truth: list[dict[str, str]] = []
    for i, case in enumerate(picked, 1):
        txn = txns.get(case["_flagged"], {})
        # Every cleared case in this history is a model false alarm, so a
        # cleared case keeps its risk_score trigger. Dressing one up as a
        # customer report would invent a case type the history does not
        # contain -- a legitimate dispute -- and score the agent against a
        # fiction. The R7 path is covered by unit tests instead.
        kind = "risk_score" if case["outcome"] == "cleared" else pick_trigger(rng)
        hid = f"HO-{i:03d}"
        pack.append({
            "case_id": hid,
            "opened_at": case["opened_at"],
            "trigger_type": kind,
            "trigger_text": trigger_text(kind, case, txn),
            "flagged_txn_id": case["_flagged"],
            "card_id": case["card_id"],
            "customer_id": case["customer_id"],
            "risk_score": (txn.get("risk_score") or "") if kind == "risk_score" else "",
        })
        truth.append({"case_id": hid, **{k: case[k] for k in TRUTH_FIELDS[1:]}})

    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "holdout_pack.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=PACK_FIELDS)
        w.writeheader()
        w.writerows(pack)
    with open(OUT / "holdout_truth.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=TRUTH_FIELDS)
        w.writeheader()
        w.writerows(truth)

    print(f"\nwrote holdout/holdout_pack.csv  ({len(pack)} cases, exam-shaped)")
    print(f"wrote holdout/holdout_truth.csv ({len(truth)} withheld outcomes)")
    print(f"\ntriggers : {dict(Counter(r['trigger_type'] for r in pack))}")
    print(f"patterns : {dict(Counter(r['pattern'] for r in truth))}")
    print(f"reports  : {sum(1 for r in truth if r['report_filed'].strip().lower() == 'yes')}"
          f" of {len(truth)}")
    print("\nNext: python src/run_cases.py --all --pack holdout/holdout_pack.csv "
          "--out holdout/answers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
