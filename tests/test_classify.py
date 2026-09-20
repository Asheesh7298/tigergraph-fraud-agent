"""Classifier tests, anchored on the two rings and on what must NOT fire."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from classify import classify, expand_episode  # noqa: E402


def txn(tid, ts, amt, channel="online", product="C", device="", addr="100.0", new=""):
    return {
        "txn_id": tid, "ts": ts, "amount": amt, "channel": channel,
        "product_cd": product, "device_profile": device, "addr1": addr,
        "device_new": new, "M4": "M0",
    }


BASELINE = {
    "p50": 58.0, "p90": 110.0, "max": 226.0,
    "products": {"W": 60}, "regions": {"100.0": 60}, "devices": {},
    "n_txns": 60,
}

RING_B = [
    txn("3476602", "2016-11-21 20:00:00", 478.95),
    txn("3476633", "2016-11-21 20:10:00", 456.96),
    txn("3476665", "2016-11-21 20:24:00", 488.04),
    txn("3476682", "2016-11-21 20:30:00", 482.12),
]


# --------------------------------------------------------------------------
# Ring B
# --------------------------------------------------------------------------

def test_ring_b_is_undocumented():
    a = classify(RING_B[3], RING_B, BASELINE)
    assert a.pattern == "undocumented"
    assert "structuring" in a.description.lower()
    assert "1,906.07" in a.description
    assert a.independent_signals >= 2


def test_ring_b_is_not_card_testing():
    """The failure mode both LLMs showed. Four large legs are not a testing run."""
    a = classify(RING_B[3], RING_B, BASELINE)
    assert a.pattern != "card_testing"


def test_three_legs_is_not_ring_b():
    a = classify(RING_B[2], RING_B[:3], BASELINE)
    assert a.pattern != "undocumented" or "structuring" not in a.description.lower()


def test_amounts_over_the_ceiling_are_not_ring_b():
    over = [dict(t, amount=520.0) for t in RING_B]
    a = classify(over[3], over, BASELINE)
    assert "structuring" not in a.description.lower()


def test_slow_burst_is_not_ring_b():
    slow = [dict(t) for t in RING_B]
    slow[3]["ts"] = "2016-11-21 23:30:00"     # three hours, not thirty minutes
    a = classify(slow[3], slow, BASELINE)
    assert "structuring" not in a.description.lower()


# --------------------------------------------------------------------------
# Ring A
# --------------------------------------------------------------------------

RING_A_TXNS = [
    txn("3460634", "2016-11-15 20:30:00", 112.37, device="RING", new="New"),
    txn("3478561", "2016-11-22 16:11:00", 74.96, device="RING", new="New"),
    txn("3489320", "2016-11-26 17:39:00", 252.28, device="RING", new="New"),
]
RING_INFO = {
    "profile": "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080",
    "n_cards": 28, "cards": ["C13487-K1", "C10326-K1", "C08168-K1"],
    "first_seen": "2016-11-14 16:54:00", "last_seen": "2016-12-04 11:47:00",
}


def test_ring_a_is_undocumented_and_names_connected_cards():
    a = classify(RING_A_TXNS[1], RING_A_TXNS, BASELINE, ring=RING_INFO, card_id="C13487-K1")
    assert a.pattern == "undocumented"
    assert a.ring_profile == RING_INFO["profile"]
    assert "C13487-K1" not in a.connected_cards, "the subject's own card is not a connection"
    assert "C10326-K1" in a.connected_cards
    assert "28" in a.description


def test_ring_a_beats_new_device_pattern():
    """All three legs are New-device online -- without ring context this would
    classify as card_not_present_new_device. The ring must win."""
    with_ring = classify(RING_A_TXNS[1], RING_A_TXNS, BASELINE, ring=RING_INFO)
    without = classify(RING_A_TXNS[1], RING_A_TXNS, BASELINE, ring=None)
    assert with_ring.pattern == "undocumented"
    assert without.pattern == "card_not_present_new_device"


def test_small_device_cluster_is_not_a_ring():
    a = classify(RING_A_TXNS[1], RING_A_TXNS, BASELINE, ring={"n_cards": 2, "cards": []})
    assert a.pattern != "undocumented"


# --------------------------------------------------------------------------
# Card testing
# --------------------------------------------------------------------------

def test_card_testing_needs_small_legs_then_a_larger_one():
    seq = [
        txn("T1", "2016-11-14 09:12:00", 1.10),
        txn("T2", "2016-11-14 09:30:00", 2.40),
        txn("T3", "2016-11-14 09:52:00", 0.95),
        txn("T4", "2016-11-14 10:31:00", 259.98),
    ]
    a = classify(seq[3], seq, BASELINE)
    assert a.pattern == "card_testing"


def test_small_legs_without_a_larger_purchase_are_not_testing():
    seq = [
        txn("T1", "2016-11-14 09:12:00", 1.10),
        txn("T2", "2016-11-14 09:30:00", 2.40),
        txn("T3", "2016-11-14 09:52:00", 0.95),
    ]
    assert classify(seq[2], seq, BASELINE).pattern != "card_testing"


# --------------------------------------------------------------------------
# Legitimate
# --------------------------------------------------------------------------

def test_baseline_consistent_activity_is_none():
    ok = [txn("T1", "2016-12-01 10:00:00", 57.99, channel="in_person", product="W")]
    a = classify(ok[0], ok, BASELINE)
    assert a.pattern == "none"
    assert a.independent_signals == 0


def test_out_of_region_needs_parallel_home_activity():
    """716 of 900 cleared cases were travel. A new region alone is not fraud."""
    t = [txn("T1", "2016-12-01 10:00:00", 90.0, channel="in_person", product="W", addr="444.0")]
    travelling = classify(t[0], t, BASELINE,
                          region={"n_in_region": 0, "n_total": 300, "parallel_elsewhere": 0})
    assert travelling.pattern != "out_of_region_use"

    cloned = classify(t[0], t, BASELINE,
                      region={"n_in_region": 0, "n_total": 300, "parallel_elsewhere": 4,
                              "parallel_txn_ids": ["X1", "X2"]})
    assert cloned.pattern == "out_of_region_use"
    assert cloned.independent_signals == 2


# --------------------------------------------------------------------------
# Episode expansion
# --------------------------------------------------------------------------

def test_episode_walks_backwards_from_the_flagged_transaction():
    """HHG-006's flagged leg is the last of four; HHG-014's is the middle of three."""
    ep = expand_episode(RING_B[3], RING_B, BASELINE)
    assert [t["txn_id"] for t in ep] == ["3476602", "3476633", "3476665", "3476682"]
    assert round(sum(float(t["amount"]) for t in ep), 2) == 1906.07


def test_episode_stops_at_an_unrelated_transaction():
    unrelated = txn("OLD", "2016-10-01 09:00:00", 58.0, channel="in_person",
                    product="W", addr="100.0")
    ep = expand_episode(RING_B[3], [unrelated] + RING_B, BASELINE)
    assert "OLD" not in [t["txn_id"] for t in ep]


def test_episode_has_no_fixed_cap():
    """A hard cap of four would miss 36.8% of fraudulent transactions."""
    long_run = [
        txn(f"T{i}", f"2016-11-21 {20 + i // 60:02d}:{i % 60:02d}:00", 60.0, device="D1")
        for i in range(30)
    ]
    ep = expand_episode(long_run[0], long_run, BASELINE)
    assert len(ep) > 4


def test_flagged_transaction_is_always_included():
    lonely = txn("ALONE", "2016-12-02 15:18:27", 1000.03)
    ep = expand_episode(lonely, [], BASELINE)
    assert [t["txn_id"] for t in ep] == ["ALONE"]
