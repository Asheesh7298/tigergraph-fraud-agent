"""Score agent answers against the withheld holdout truth.

    python holdout/score.py

Reports what the exam scores: verdict accuracy, pattern accuracy, precision
and recall on the transactions named as the fraud episode, precision and
recall on the report decision, and calibration error.

Calibration matters on its own. `fraud_probability` is scored for it, and a
submission where every case reads 0.9 is not calibrated however often the
verdict is right -- so the report breaks probability into buckets and shows
the observed fraud rate in each.

The holdout narratives are templated, so these numbers read optimistically
against the real exam. Direction of error is the signal: over-blocking,
over-reporting, or systematic over-confidence will show here even though the
absolute accuracy will not hold up.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from schema import Answer  # noqa: E402

csv.field_size_limit(10_000_000)


def truth_verdict(row: dict[str, str]) -> str:
    return "fraud" if row["outcome"] == "confirmed_fraud" else "legitimate"


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def bar(x: float, width: int = 28) -> str:
    n = int(round(x * width))
    return "#" * n + "." * (width - n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--answers", type=Path, default=ROOT / "holdout" / "answers")
    ap.add_argument("--truth", type=Path, default=ROOT / "holdout" / "holdout_truth.csv")
    ap.add_argument("--json", type=Path, help="also write the report as JSON")
    args = ap.parse_args()

    truth = {r["case_id"]: r for r in csv.DictReader(open(args.truth, encoding="utf-8"))}
    answers: dict[str, Answer] = {}
    for cid in truth:
        p = args.answers / f"{cid}.json"
        if p.exists():
            answers[cid] = Answer.read(p)
    print(f"scored {len(answers)}/{len(truth)} cases\n")
    if not answers:
        print("No answers found. Run the agent against holdout/holdout_pack.csv first.")
        return 1

    # -- verdict
    v_ok = 0
    confusion: Counter = Counter()
    for cid, a in answers.items():
        want = truth_verdict(truth[cid])
        got = a.case.verdict
        confusion[(want, got)] += 1
        v_ok += got == want
    print("VERDICT")
    print(f"  accuracy {v_ok}/{len(answers)} = {v_ok / len(answers):.1%}")
    for want in ("fraud", "legitimate"):
        line = "  ".join(
            f"{got[:4]}={confusion[(want, got)]:<3}"
            for got in ("fraud", "legitimate", "uncertain")
        )
        print(f"    truth {want:<11} -> {line}")
    # `uncertain` is a legitimate answer on ambiguous cases, so count it apart
    unc = sum(n for (w, g), n in confusion.items() if g == "uncertain")
    if unc:
        print(f"    ({unc} answered uncertain; valid per the README when actions follow R1/R8)")

    # -- pattern, on confirmed fraud only
    print("\nPATTERN  (confirmed-fraud cases only)")
    p_ok = p_n = 0
    missed: Counter = Counter()
    for cid, a in answers.items():
        t = truth[cid]
        if t["outcome"] != "confirmed_fraud":
            continue
        p_n += 1
        if a.case.pattern == t["pattern"]:
            p_ok += 1
        else:
            missed[(t["pattern"], a.case.pattern)] += 1
    if p_n:
        print(f"  accuracy {p_ok}/{p_n} = {p_ok / p_n:.1%}")
        for (want, got), n in missed.most_common(6):
            print(f"    {want} -> {got}: {n}")

    # -- affected transactions
    print("\nAFFECTED TRANSACTIONS")
    tp = fp = fn = 0
    for cid, a in answers.items():
        want = {t for t in truth[cid]["txn_ids"].split("|") if t}
        got = set(a.case.affected_txn_ids)
        tp += len(want & got)
        fp += len(got - want)
        fn += len(want - got)
    p, r, f = prf(tp, fp, fn)
    print(f"  precision {p:.1%}  recall {r:.1%}  f1 {f:.1%}   (tp {tp}, fp {fp}, fn {fn})")

    # -- report decision
    print("\nSUSPICIOUS ACTIVITY REPORT")
    tp = fp = fn = tn = 0
    for cid, a in answers.items():
        want = truth[cid]["report_filed"].strip().lower() == "yes"
        got = a.sar.file
        tp += want and got
        fp += (not want) and got
        fn += want and (not got)
        tn += (not want) and (not got)
    p, r, f = prf(tp, fp, fn)
    print(f"  precision {p:.1%}  recall {r:.1%}  f1 {f:.1%}")
    print(f"  filed {tp + fp}, should have filed {tp + fn}   "
          f"(tp {tp}, fp {fp}, fn {fn}, tn {tn})")
    if fp > tp:
        print("  OVER-REPORTING: more false filings than true ones")

    # -- exposure
    print("\nEXPOSURE")
    diffs = []
    for cid, a in answers.items():
        want = float(truth[cid]["exposure_usd"] or 0)
        if truth_verdict(truth[cid]) == "fraud":
            diffs.append(a.case.exposure_usd - want)
    if diffs:
        over = sum(1 for d in diffs if d > 1)
        under = sum(1 for d in diffs if d < -1)
        print(f"  median error ${sorted(diffs)[len(diffs) // 2]:+,.2f}   "
              f"over on {over}, under on {under}, exact on {len(diffs) - over - under}")

    # -- calibration
    print("\nCALIBRATION")
    buckets: dict[str, list[int]] = defaultdict(list)
    err = 0.0
    for cid, a in answers.items():
        actual = 1 if truth_verdict(truth[cid]) == "fraud" else 0
        err += abs(a.case.fraud_probability - actual)
        lo = min(int(a.case.fraud_probability * 5), 4) * 0.2
        buckets[f"{lo:.1f}-{lo + 0.2:.1f}"].append(actual)
    for band in sorted(buckets):
        vals = buckets[band]
        rate = sum(vals) / len(vals)
        print(f"  p {band}  n={len(vals):<3} actually fraud {rate:5.0%}  {bar(rate)}")
    print(f"  mean absolute error {err / len(answers):.3f}")

    # -- actions
    print("\nACTIONS")
    blocked = sum(1 for a in answers.values()
                  if any(x.action == "BLOCK_CARD" for x in a.next_best_actions.final))
    legit = sum(1 for cid in answers if truth_verdict(truth[cid]) == "legitimate")
    wrong_block = sum(
        1 for cid, a in answers.items()
        if truth_verdict(truth[cid]) == "legitimate"
        and any(x.action == "BLOCK_CARD" for x in a.next_best_actions.final)
    )
    print(f"  blocked {blocked}/{len(answers)} cards")
    print(f"  blocked on legitimate activity: {wrong_block}/{legit}"
          + ("   <-- policy breach under R1" if wrong_block else ""))
    changed = sum(1 for a in answers.values()
                  if a.next_best_actions.what_changed.strip().lower() != "nothing")
    print(f"  recommendation changed after evidence in {changed}/{len(answers)}")

    if args.json:
        args.json.write_text(json.dumps({
            "n": len(answers),
            "verdict_accuracy": v_ok / len(answers),
            "pattern_accuracy": (p_ok / p_n) if p_n else None,
            "calibration_mae": err / len(answers),
            "blocked_legitimate": wrong_block,
        }, indent=2), encoding="utf-8")

    print("\nNote: holdout narratives are templated, so these read optimistically")
    print("against the exam. Direction of error is the signal, not the level.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
