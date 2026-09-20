"""Narrow the raw CSVs into graph-loadable files.

transactions.csv is 675 MB of 397 columns, 339 of which are Vesta's unnamed
engineered features (V1-V339). Those cannot be honestly cited as evidence --
"V127 was elevated" means nothing to a fraud analyst or a regulator -- so they
are dropped. Every row is kept: fewer rows would break ring detection and
customer baselines, which are the two things the investigation actually turns
on. 397 columns -> 59 is a 6.8x reduction with no investigative signal lost,
and it is what makes loading the graph a task of minutes rather than hours.

Outputs, all under data/:
  txn_narrow.csv       one row per transaction, with card_id attached
  identity_narrow.csv  one row per identity record, with device_profile
  device_profiles.csv  one row per distinct device profile
  id_registry.json     every ID that may legally appear in an answer file
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import card_id as cid  # noqa: E402
from schema import IdRegistry  # noqa: E402

csv.field_size_limit(10_000_000)

KEEP = (
    ["TransactionID", "TransactionDT", "TransactionAmt", "ProductCD"]
    + [f"card{i}" for i in range(1, 7)]
    + ["addr1", "addr2", "dist1", "dist2", "P_emaildomain", "R_emaildomain"]
    + [f"C{i}" for i in range(1, 15)]      # entity counts
    + [f"D{i}" for i in range(1, 16)]      # day deltas
    + [f"M{i}" for i in range(1, 10)]      # match flags
    + ["customer_id", "ts", "channel", "risk_score"]
)
OUT_COLS = KEEP + ["card_id"]

#: The README defines a device profile as DeviceInfo + OS + browser + screen,
#: and that is the string `connected_device_profiles` expects. The proxy flag
#: (id_23) is kept as a separate attribute rather than folded into the key --
#: it is a strong ring discriminator but not part of the published format.
PROFILE_PARTS = ("DeviceInfo", "id_30", "id_31", "id_33")
IDENTITY_KEEP = [
    "TransactionID", "DeviceType", "DeviceInfo",
    "id_12", "id_15", "id_16", "id_23", "id_28", "id_29",
    "id_30", "id_31", "id_33", "id_34", "id_35", "id_36", "id_37", "id_38",
]


def blank(v: str | None) -> str:
    """Vesta writes missing values as empty, 'NaN' or 'nan' depending on column."""
    v = (v or "").strip()
    return "" if v.lower() in ("", "nan", "none", "null") else v


def profile_key(row: dict[str, str]) -> str:
    parts = [blank(row.get(p)) for p in PROFILE_PARTS]
    return " | ".join(p if p else "unknown" for p in parts)


def narrow_transactions(raw: Path, out: Path, card_map: dict[str, str]) -> tuple[int, set, set, set]:
    txn_ids: set[str] = set()
    card_ids: set[str] = set()
    customer_ids: set[str] = set()
    n = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(raw, encoding="utf-8", newline="") as fh, \
            open(out, "w", encoding="utf-8", newline="") as fo:
        reader = csv.DictReader(fh)
        writer = csv.DictWriter(fo, fieldnames=OUT_COLS, extrasaction="ignore")
        writer.writeheader()
        for row in reader:
            n += 1
            row["card_id"] = cid.card_id_for(card_map, row)
            txn_ids.add(row["TransactionID"])
            card_ids.add(row["card_id"])
            customer_ids.add(row["customer_id"])
            writer.writerow({k: row.get(k, "") for k in OUT_COLS})
    return n, txn_ids, card_ids, customer_ids


def narrow_identity(raw: Path, out: Path, profiles_out: Path) -> tuple[int, set[str], Counter]:
    profiles: set[str] = set()
    proxy_of: dict[str, Counter] = defaultdict(Counter)
    n = 0
    with open(raw, encoding="utf-8", newline="") as fh, \
            open(out, "w", encoding="utf-8", newline="") as fo:
        reader = csv.DictReader(fh)
        writer = csv.DictWriter(
            fo, fieldnames=IDENTITY_KEEP + ["device_profile"], extrasaction="ignore"
        )
        writer.writeheader()
        for row in reader:
            n += 1
            key = profile_key(row)
            profiles.add(key)
            proxy_of[key][blank(row.get("id_23")) or "none"] += 1
            out_row = {k: blank(row.get(k)) for k in IDENTITY_KEEP}
            out_row["TransactionID"] = row["TransactionID"]
            out_row["device_profile"] = key
            writer.writerow(out_row)

    with open(profiles_out, "w", encoding="utf-8", newline="") as fo:
        w = csv.writer(fo)
        w.writerow(["device_profile", "device_info", "os", "browser", "screen",
                    "dominant_proxy", "n_records"])
        for key in sorted(profiles):
            info, os_, br, sc = key.split(" | ")
            proxy, _ = proxy_of[key].most_common(1)[0]
            w.writerow([key, info, os_, br, sc, proxy, sum(proxy_of[key].values())])
    return n, profiles, Counter()


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw", type=Path, default=root / "data" / "raw")
    ap.add_argument("--out", type=Path, default=root / "data")
    args = ap.parse_args()

    raw, out = args.raw, args.out

    cm_path = out / "card_map.json"
    if cm_path.exists():
        card_map = cid.load(cm_path)
        print(f"card map: reusing {len(card_map):,} entries from {cm_path.name}")
    else:
        print("card map: building...", flush=True)
        card_map = cid.build_card_map(raw / "transactions.csv")
        cid.save(card_map, cm_path)
        print(f"  {len(card_map):,} entries")

    print("transactions: narrowing 397 -> 59 columns...", flush=True)
    n, txn_ids, card_ids, customer_ids = narrow_transactions(
        raw / "transactions.csv", out / "txn_narrow.csv", card_map
    )
    size = (out / "txn_narrow.csv").stat().st_size / 1e6
    print(f"  {n:,} rows -> {size:,.0f} MB ({len(card_ids):,} cards, {len(customer_ids):,} customers)")
    if "" in card_ids:
        print("  WARNING: some transactions did not resolve to a card_id")
        card_ids.discard("")

    print("identity: narrowing and keying device profiles...", flush=True)
    ni, profiles, _ = narrow_identity(
        raw / "identity.csv", out / "identity_narrow.csv", out / "device_profiles.csv"
    )
    print(f"  {ni:,} records -> {len(profiles):,} distinct device profiles")

    closed_ids = {
        r["case_id"]
        for r in csv.DictReader(open(raw / "closed_cases_history.csv", encoding="utf-8"))
    }
    case_ids = {
        r["case_id"]
        for r in csv.DictReader(open(raw / "case_pack.csv", encoding="utf-8"))
    }
    IdRegistry(txn_ids, card_ids, customer_ids, closed_ids, profiles, case_ids).save(out)
    print(f"id registry: {len(txn_ids):,} txns, {len(card_ids):,} cards, "
          f"{len(customer_ids):,} customers, {len(closed_ids):,} closed cases, "
          f"{len(profiles):,} profiles")

    print("\nself-check (strict: flagged txn -> card_id)...", flush=True)
    pk, pk_n, cl, cl_n, problems = cid.self_check(
        card_map, raw / "transactions.csv", raw / "case_pack.csv",
        raw / "closed_cases_history.csv",
    )
    for line in problems[:10]:
        print(line)
    print(f"  case pack:         {pk}/{pk_n}")
    print(f"  closed-case cross: {cl}/{cl_n}")
    if pk != pk_n or cl != cl_n:
        print("\nSTOP. The card map is wrong. Every answer file depends on it.")
        return 1
    print("\nOK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
