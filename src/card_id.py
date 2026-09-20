"""Reconstruct the `card_id` values (e.g. C12382-K1) used by the case pack.

`card_id` is not a column in transactions.csv. It has to be rebuilt from
(customer_id, card fingerprint). Getting this wrong silently poisons every
answer file, so the rule and its verification both live here.

THE RULE -- verified 20/20 on the case pack AND 4665/4665 on the closed-case
history:

    A card is identified by (card1, card4, card6): the issuer code, the
    network, and the card type. Within a customer, sort the distinct
    fingerprints LEXICOGRAPHICALLY ASCENDING and number them K1, K2, ...

There is no temporal component. Two plausible-looking alternatives were
tested and rejected:

  * Ranking by first-appearance timestamp (either direction) fits the 20
    pack cases but fails 309 closed cases -- the pack alone cannot
    distinguish the rules, which is why the closed-case cross-check exists.
  * Using all six card columns over-splits: card2/card3/card5 vary within a
    single physical card, producing spurious K3/K4 (4440/4665).

Lexicographic ordering is what makes `credit` sort before `debit`, and makes
a sparse fingerprint with blank card4/card6 sort first.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(10_000_000)

#: The columns that identify a physical card. Not all six -- see module docstring.
FP_COLS = ("card1", "card4", "card6")

Fingerprint = tuple[str, ...]


def fingerprint(row: dict[str, str]) -> Fingerprint:
    """The card identity of a transaction row, as a hashable tuple."""
    return tuple((row.get(c) or "").strip() for c in FP_COLS)


def build_card_map(transactions_csv: Path) -> dict[str, str]:
    """One pass over transactions.csv -> {"customer_id|card1|card4|card6": card_id}.

    Keys are flattened to strings so the map round-trips through JSON.
    """
    seen: dict[str, set[Fingerprint]] = defaultdict(set)
    with open(transactions_csv, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            seen[row["customer_id"]].add(fingerprint(row))

    card_map: dict[str, str] = {}
    for cust, fps in seen.items():
        for i, fp in enumerate(sorted(fps)):
            card_map[key_for(cust, fp)] = f"{cust}-K{i + 1}"
    return card_map


def key_for(customer_id: str, fp: Fingerprint) -> str:
    return "|".join((customer_id,) + fp)


def card_id_for(card_map: dict[str, str], row: dict[str, str]) -> str:
    return card_map.get(key_for(row["customer_id"], fingerprint(row)), "")


def save(card_map: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(card_map), encoding="utf-8")


def load(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def self_check(
    card_map: dict[str, str],
    transactions_csv: Path,
    case_pack_csv: Path,
    closed_cases_csv: Path | None = None,
) -> tuple[int, int, int, int, list[str]]:
    """Strict check: does each case's FLAGGED TRANSACTION map to its card_id?

    A membership test ("is C07297-K1 among this customer's reconstructed
    ids?") is vacuous for a two-card customer -- it passes for either
    assignment. Only resolving the flagged transaction's own fingerprint
    actually tests the rule.

    The closed-case cross-check matters just as much: it exercises the
    mapping 200x more widely than the pack, and a mismatch there means
    ClosedCase -> ON_CARD -> Card will not link in the graph, silently
    breaking case memory.

    Returns (pack_passed, pack_total, closed_passed, closed_total, problems).
    """
    want = {
        r["flagged_txn_id"]: (r["case_id"], r["card_id"])
        for r in csv.DictReader(open(case_pack_csv, encoding="utf-8"))
    }

    closed: dict[str, tuple[str, str]] = {}
    if closed_cases_csv is not None:
        for r in csv.DictReader(open(closed_cases_csv, encoding="utf-8")):
            if r["first_fraud_txn_id"]:
                closed[r["first_fraud_txn_id"]] = (r["case_id"], r["card_id"])

    got: dict[str, str] = {}
    with open(transactions_csv, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            tid = row["TransactionID"]
            if tid in want or tid in closed:
                got[tid] = card_id_for(card_map, row)

    problems: list[str] = []
    pack_passed = 0
    for tid, (case_id, expected) in sorted(want.items()):
        actual = got.get(tid, "<txn not found>")
        if actual == expected:
            pack_passed += 1
        else:
            problems.append(f"  {case_id}: txn {tid} -> {actual!r}, pack says {expected!r}")

    closed_passed = 0
    for tid, (case_id, expected) in closed.items():
        if got.get(tid) == expected:
            closed_passed += 1
        elif len(problems) < 25:
            problems.append(
                f"  {case_id} (closed): txn {tid} -> {got.get(tid)!r}, history says {expected!r}"
            )

    return pack_passed, len(want), closed_passed, len(closed), problems


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    raw = root / "data" / "raw"

    print("Building card map (one pass over transactions.csv)...", flush=True)
    card_map = build_card_map(raw / "transactions.csv")
    print(f"  {len(card_map):,} distinct (customer, card) pairs")

    out = root / "data" / "card_map.json"
    save(card_map, out)
    print(f"  wrote {out.relative_to(root)}")

    print("Self-check (strict: flagged txn -> card_id)...", flush=True)
    pk, pk_n, cl, cl_n, problems = self_check(
        card_map,
        raw / "transactions.csv",
        raw / "case_pack.csv",
        raw / "closed_cases_history.csv",
    )
    for line in problems:
        print(line)
    print(f"\n  case pack:          {pk}/{pk_n}")
    print(f"  closed-case cross:  {cl}/{cl_n}")

    if pk != pk_n or cl != cl_n:
        print("\nSTOP. The card map is wrong. Every answer file depends on it.")
        return 1
    print("\nOK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
