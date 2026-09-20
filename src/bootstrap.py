"""Emit twenty schema-valid answer files from case_pack.csv alone.

No graph, no LLM, no network. The point is that a submission-shaped set of
files exists from day one and every later stage overwrites rather than
creates. A half-finished agent then degrades to placeholder answers instead
of missing files, and `validate_all` can run against the real thing from the
start.

    python src/bootstrap.py              # write placeholders
    python src/bootstrap.py --validate   # check whatever is in cases/
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from policy import Assessment, decide, should_file_sar, should_stop, status_for  # noqa: E402
from schema import (  # noqa: E402
    Answer,
    Case,
    Evidence,
    IdRegistry,
    NextBestActions,
    SAR,
    check_exposure,
)

PLACEHOLDER = "Placeholder: the agent has not investigated this case yet."


def load_pack(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def chronological(pack: list[dict[str, str]]) -> list[dict[str, str]]:
    """Process order for the real run.

    Sorting by `opened_at` puts HHG-014 -- the ring case -- fourth, so the
    sixteen investigations after it can retrieve it from the graph and cite it
    in `similar_prior_cases`. Running the pack in parallel would leave nothing
    to show for the case-memory criterion. The cost is one sorted() call.
    """
    return sorted(pack, key=lambda r: (r["opened_at"], r["case_id"]))


def placeholder_answer(row: dict[str, str]) -> Answer:
    disputed = row["trigger_type"] == "customer_report"
    a = Assessment(
        verdict="uncertain",
        fraud_probability=0.50,
        pattern="none",
        customer_disputed=disputed,
        independent_signals=1,
    )
    actions = decide(a)
    file_sar, sar_reason = should_file_sar(a)
    assert not file_sar, "a placeholder must never file a report"

    recs = [{"action": r.action, "route": r.route, "reason": r.reason} for r in actions]
    return Answer(
        case_id=row["case_id"],
        case=Case(
            status=status_for(a, actions, evidence_pending=True),
            verdict=a.verdict,
            fraud_probability=a.fraud_probability,
            pattern=a.pattern,
            affected_txn_ids=[],
            first_suspicious_txn_id="",
            exposure_usd=0.0,
            evidence=[
                Evidence(
                    claim=f"{PLACEHOLDER} Trigger was {row['trigger_type']}.",
                    source="external",
                    ref="case_pack.csv",
                    entity_ids=[row["flagged_txn_id"]],
                )
            ],
            summary=f"{PLACEHOLDER} Flagged transaction {row['flagged_txn_id']} "
                    f"on card {row['card_id']}.",
            written_to_graph=False,
        ),
        evidence_requests=[],
        next_best_actions=NextBestActions(initial=recs, final=recs, what_changed="nothing"),
        sar=SAR(file=False, reason=sar_reason),
        stop_reason=should_stop(a)[1],
        tool_calls=0,
        tokens=0,
        latency_s=0.0,
    )


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def amounts_for(narrow_csv: Path, wanted: set[str]) -> dict[str, float]:
    if not narrow_csv.exists():
        return {}
    out: dict[str, float] = {}
    with open(narrow_csv, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["TransactionID"] in wanted:
                out[r["TransactionID"]] = float(r["TransactionAmt"])
    return out


def distribution_problems(answers: list[Answer]) -> list[str]:
    """Sanity-check the shape of the twenty verdicts before submitting.

    Grounded in the closed-case history and the README's statement that half
    the cases are legitimate. These are warnings about the submission as a
    whole, not about any single file.
    """
    p: list[str] = []
    verdicts = Counter(a.case.verdict for a in answers)
    patterns = Counter(a.case.pattern for a in answers)
    sars = sum(1 for a in answers if a.sar.file)

    if verdicts["fraud"] > 12:
        p.append(f"{verdicts['fraud']} fraud verdicts: the README says about half are legitimate")
    if verdicts["legitimate"] < 5:
        p.append(f"only {verdicts['legitimate']} legitimate verdicts: expected roughly 7-10")
    if patterns["card_testing"] > 1:
        p.append(
            f"{patterns['card_testing']} card_testing: it is 0.34% of confirmed history "
            f"(16 of 4,665). More than one suggests the README example was pattern-matched"
        )
    if patterns["undocumented"] == 0:
        p.append("no undocumented pattern: HHG-006 is Ring B and HHG-014 is Ring A")
    if sars > 8:
        p.append(f"{sars} reports filed: expected roughly 2-5 of twenty")
    return p


def validate_all(cases_dir: Path, data_dir: Path, pack: list[dict[str, str]]) -> list[str]:
    problems: list[str] = []
    answers: list[Answer] = []

    registry = None
    if (data_dir / "id_registry.json").exists():
        registry = IdRegistry.from_disk(data_dir)

    for row in pack:
        path = cases_dir / f"{row['case_id']}.json"
        if not path.exists():
            problems.append(f"{row['case_id']}: missing {path.name}")
            continue
        try:
            a = Answer.read(path)
        except Exception as exc:                      # noqa: BLE001
            problems.append(f"{row['case_id']}: invalid -- {exc}")
            continue
        if a.case_id != row["case_id"]:
            problems.append(f"{row['case_id']}: case_id says {a.case_id!r}")
        answers.append(a)

    wanted = {t for a in answers for t in a.case.affected_txn_ids}
    amounts = amounts_for(data_dir / "txn_narrow.csv", wanted)
    for a in answers:
        if amounts:
            problems += check_exposure(a, amounts)
        if registry is not None:
            problems += registry.check(a)

    if len(answers) == len(pack):
        problems += [f"distribution: {w}" for w in distribution_problems(answers)]
    return problems


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case-pack", type=Path, default=root / "data" / "raw" / "case_pack.csv")
    ap.add_argument("--out", type=Path, default=root / "cases")
    ap.add_argument("--data", type=Path, default=root / "data")
    ap.add_argument("--validate", action="store_true", help="validate only, write nothing")
    ap.add_argument("--force", action="store_true", help="overwrite real answers with placeholders")
    args = ap.parse_args()

    pack = load_pack(args.case_pack)

    if not args.validate:
        written = skipped = 0
        for row in pack:
            path = args.out / f"{row['case_id']}.json"
            if path.exists() and not args.force:
                try:
                    existing = Answer.read(path)
                    if PLACEHOLDER not in existing.case.summary:
                        skipped += 1
                        continue
                except Exception:                      # noqa: BLE001
                    pass                               # unreadable: overwrite it
            placeholder_answer(row).write(path)
            written += 1
        print(f"{written} placeholder file(s) written, {skipped} real answer(s) left alone")
        print("\nChronological processing order (HHG-014 lands 4th):")
        print("  " + " -> ".join(r["case_id"] for r in chronological(pack)))

    problems = validate_all(args.out, args.data, pack)
    print()
    if problems:
        for p in problems:
            print(f"  {p}")
        print(f"\n{len(problems)} problem(s).")
        return 1
    print(f"{len(pack)} files. All schema-valid, all invariants satisfied. 0 problems.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
