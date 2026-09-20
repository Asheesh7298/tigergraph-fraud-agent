"""Facts the investigation depends on, re-derived from the narrowed data.

These are not unit tests of our code so much as regression tests on the
dataset's shape. Two of the twenty answers (HHG-006 and HHG-014) rest
entirely on the ring findings below, and the R7 gate rests on the recurrence
counts. If a prep change quietly alters any of them, that must fail loudly
here rather than surface as a wrong verdict in a submitted file.

Skipped when data/ has not been built yet (it is gitignored).
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DATA = ROOT / "data"
NARROW = DATA / "txn_narrow.csv"
IDENT = DATA / "identity_narrow.csv"

pytestmark = pytest.mark.skipif(
    not NARROW.exists() or not IDENT.exists(),
    reason="run `python src/prep_data.py` first",
)

csv.field_size_limit(10_000_000)

RING_A_DEVICE = "SM-G935F Build/NRD90M"
RING_A_OS = "Android 7.0"
RING_A_BROWSER = "chrome 62.0 for android"
RING_A_PROXY = "IP_PROXY:ANONYMOUS"


@pytest.fixture(scope="module")
def txns():
    with open(NARROW, encoding="utf-8", newline="") as fh:
        return {r["TransactionID"]: r for r in csv.DictReader(fh)}


@pytest.fixture(scope="module")
def identity():
    with open(IDENT, encoding="utf-8", newline="") as fh:
        return {r["TransactionID"]: r for r in csv.DictReader(fh)}


# --------------------------------------------------------------------------
# HHG-006 -- Ring B, threshold structuring
# --------------------------------------------------------------------------

def test_hhg006_is_a_four_transaction_structuring_burst(txns):
    flagged = txns["3476682"]
    assert flagged["card_id"] == "C07297-K1"

    same_card = [t for t in txns.values() if t["card_id"] == "C07297-K1"]
    day = [t for t in same_card if t["ts"].startswith("2016-11-21")]
    burst = sorted(
        (t for t in day if "20:00:00" <= t["ts"][11:] <= "20:45:00"),
        key=lambda t: t["ts"],
    )

    assert [t["TransactionID"] for t in burst] == ["3476602", "3476633", "3476665", "3476682"]
    amounts = [round(float(t["TransactionAmt"]), 2) for t in burst]
    assert amounts == [478.95, 456.96, 488.04, 482.12]
    assert round(sum(amounts), 2) == 1906.07

    # The Ring B signature: every leg deliberately under a $500 ceiling,
    # all online, one product code, inside three quarters of an hour.
    assert all(450.0 <= a < 500.0 for a in amounts)
    assert {t["channel"] for t in burst} == {"online"}
    assert {t["ProductCD"] for t in burst} == {"C"}


def test_hhg006_flagged_transaction_precedes_the_case_opening(txns):
    """The alert opened at 02:30 on the 22nd; the activity was the evening before.

    Windowing off `opened_at` instead of the transaction's own ts would miss
    the entire episode.
    """
    assert txns["3476682"]["ts"] == "2016-11-21 20:30:00"


# --------------------------------------------------------------------------
# HHG-014 -- Ring A, shared anonymised-proxy device
# --------------------------------------------------------------------------

def test_hhg014_sits_on_the_ring_a_device(txns, identity):
    flagged = txns["3478561"]
    assert flagged["card_id"] == "C13487-K1"
    ident = identity["3478561"]
    assert ident["DeviceInfo"] == RING_A_DEVICE
    assert ident["id_30"] == RING_A_OS
    assert ident["id_31"] == RING_A_BROWSER
    assert ident["id_23"] == RING_A_PROXY
    assert ident["id_15"] == "New"


def test_ring_a_spans_many_cards_in_the_exam_window(txns, identity):
    """The finding HHG-014's analyst request is pointing at."""
    cards, txn_ids = set(), []
    for tid, ident in identity.items():
        if (
            ident["DeviceInfo"] == RING_A_DEVICE
            and ident["id_30"] == RING_A_OS
            and ident["id_31"] == RING_A_BROWSER
            and ident["id_23"] == RING_A_PROXY
        ):
            t = txns.get(tid)
            if t and t["ts"] >= "2016-11-01":
                cards.add(t["card_id"])
                txn_ids.append(tid)

    assert len(cards) == 28, f"Ring A spans 28 cards in Nov-Dec, got {len(cards)}"
    assert "C13487-K1" in cards
    assert len(txn_ids) == 60


def test_ring_a_predates_the_exam_window(txns, identity):
    """The ring runs back into the closed-case period, which is what lets the
    agent cite CC-2649 / CC-2971 / CC-2985 / CC-3035 as memory rather than
    presenting HHG-014 as something new."""
    stamps = [
        txns[tid]["ts"]
        for tid, ident in identity.items()
        if tid in txns
        and ident["DeviceInfo"] == RING_A_DEVICE
        and ident["id_30"] == RING_A_OS
        and ident["id_31"] == RING_A_BROWSER
        and ident["id_23"] == RING_A_PROXY
    ]
    assert min(stamps) < "2016-11-01", "ring should have history before the exam window"


def test_ring_a_subject_card_has_three_transactions(txns, identity):
    """All three belong in affected_txn_ids; only the middle one was flagged.

    The episode walk has to run backwards as well as forwards -- the flagged
    transaction is neither the first nor the largest.
    """
    on_card = sorted(
        (t for t in txns.values() if t["card_id"] == "C13487-K1" and t["ts"] >= "2016-11-01"),
        key=lambda t: t["ts"],
    )
    ring = [t for t in on_card if identity.get(t["TransactionID"], {}).get("id_23") == RING_A_PROXY]
    assert [t["TransactionID"] for t in ring] == ["3460634", "3478561", "3489320"]
    assert round(sum(float(t["TransactionAmt"]) for t in ring), 2) == 439.61


def test_ring_a_transactions_scored_low(txns):
    """The reason this case needed an analyst rather than the model.

    Every leg scored below 0.25. No risk-score trigger would ever have raised
    it; only the shared device profile connects them.
    """
    for tid in ("3460634", "3478561", "3489320"):
        assert float(txns[tid]["risk_score"]) < 0.25


def test_ring_a_breaks_the_cardholder_baseline(txns):
    """Six months of in-person spend, then three online purchases on a new device."""
    before = [
        t for t in txns.values()
        if t["card_id"] == "C13487-K1" and t["ts"] < "2016-11-15"
    ]
    online = [t for t in before if t["channel"] == "online"]
    assert len(before) > 50
    assert len(online) <= 1, "card is essentially in-person only before the ring activity"
    assert {t["ProductCD"] for t in ("3460634", "3478561", "3489320")
            for t in [txns[t]]} == {"C"}


def test_device_model_alone_is_not_ring_membership(txns, identity):
    """HHG-008 and HHG-011 share the phone model but not the profile.

    Matching on DeviceInfo substring pulls them in falsely; the Galaxy S7 was
    one of the most common handsets of 2016. Ring membership needs the OS,
    browser and proxy flag too.
    """
    for tid in ("3558054", "3583368"):          # HHG-008, HHG-011
        ident = identity.get(tid)
        if ident is None:
            continue
        if RING_A_DEVICE in ident["DeviceInfo"]:
            assert ident["id_23"] != RING_A_PROXY, f"{tid} must not match the full fingerprint"


# --------------------------------------------------------------------------
# R7 -- recurrence behind the customer-report cases
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case, txn_id, card, at_least",
    [
        ("HHG-018", "3491361", "C02354-K2", 100),   # ~170 occurrences
        ("HHG-003", "3530164", "C08623-K2", 50),    # ~82
        ("HHG-008", "3558054", "C13171-K2", 10),    # ~19
    ],
)
def test_established_recurring_charges(txns, case, txn_id, card, at_least):
    flagged = txns[txn_id]
    assert flagged["card_id"] == card
    amt, product = round(float(flagged["TransactionAmt"]), 2), flagged["ProductCD"]
    prior = [
        t for t in txns.values()
        if t["card_id"] == card
        and t["ProductCD"] == product
        and abs(round(float(t["TransactionAmt"]), 2) - amt) <= 2.0
        and t["ts"] < flagged["ts"]
    ]
    assert len(prior) >= at_least, f"{case}: expected >={at_least} prior, got {len(prior)}"


@pytest.mark.parametrize(
    "case, txn_id, card, at_most",
    [
        ("HHG-009", "3581141", "C08299-K1", 5),
        ("HHG-016", "3534820", "C09988-K1", 5),
    ],
)
def test_sparse_charges_do_not_establish_recurrence(txns, case, txn_id, card, at_most):
    """These two must fall to R1/R8, not be waved through by R7."""
    flagged = txns[txn_id]
    amt, product = round(float(flagged["TransactionAmt"]), 2), flagged["ProductCD"]
    prior = [
        t for t in txns.values()
        if t["card_id"] == card
        and t["ProductCD"] == product
        and abs(round(float(t["TransactionAmt"]), 2) - amt) <= 2.0
        and t["ts"] < flagged["ts"]
    ]
    assert len(prior) <= at_most, f"{case}: expected <={at_most} prior, got {len(prior)}"


# --------------------------------------------------------------------------
# HHG-010 -- isolated outlier, single signal
# --------------------------------------------------------------------------

def test_hhg010_is_an_isolated_outlier(txns):
    flagged = txns["3506725"]
    assert flagged["card_id"] == "C10434-K1"
    assert round(float(flagged["TransactionAmt"]), 2) == 1000.03

    history = [
        t for t in txns.values()
        if t["card_id"] == "C10434-K1" and t["ts"] < flagged["ts"]
    ]
    assert history, "card should have prior history"
    prior_max = max(round(float(t["TransactionAmt"]), 2) for t in history)
    assert prior_max < 700, "the flagged amount should far exceed anything before it"

    nearby = [
        t for t in txns.values()
        if t["card_id"] == "C10434-K1"
        and t["TransactionID"] != flagged["TransactionID"]
        and "2016-11-30" <= t["ts"] <= "2016-12-04"
    ]
    assert nearby == [], "no episode to expand -- this is a single signal, so R1 governs"


# --------------------------------------------------------------------------
# Sanity on the pack as a whole
# --------------------------------------------------------------------------

def test_every_pack_transaction_resolves(txns):
    pack = list(csv.DictReader(open(ROOT / "data" / "raw" / "case_pack.csv", encoding="utf-8")))
    assert len(pack) == 20
    for r in pack:
        t = txns.get(r["flagged_txn_id"])
        assert t is not None, f"{r['case_id']}: flagged transaction missing"
        assert t["card_id"] == r["card_id"], f"{r['case_id']}: card_id mismatch"
        assert t["customer_id"] == r["customer_id"]


def test_six_pack_cases_are_in_person(txns, identity):
    """No identity record means no device evidence is available for those."""
    pack = list(csv.DictReader(open(ROOT / "data" / "raw" / "case_pack.csv", encoding="utf-8")))
    without = {r["case_id"] for r in pack if r["flagged_txn_id"] not in identity}
    assert without == {"HHG-001", "HHG-002", "HHG-003", "HHG-007", "HHG-012", "HHG-018"}
