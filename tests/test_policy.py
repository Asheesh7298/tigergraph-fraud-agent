"""Policy engine tests.

The anchor is the README's worked example (HHG-017, card testing): if the
engine cannot reproduce the one complete answer the organisers published, it
will not reproduce the twenty they are grading.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from policy import (  # noqa: E402
    Assessment,
    decide,
    recurrence_check,
    route_for,
    should_file_sar,
    should_stop,
    status_for,
)


def names(recs):
    return [r.action for r in recs]


def routes(recs):
    return {r.action: r.route for r in recs}


# --------------------------------------------------------------------------
# Routing -- policy section 2
# --------------------------------------------------------------------------

def test_block_card_route_boundary():
    assert route_for("BLOCK_CARD", 2_500.00) == "L1"
    assert route_for("BLOCK_CARD", 2_500.01) == "L2"


def test_fixed_routes():
    assert route_for("FILE_REPORT", 0.0) == "L2"
    assert route_for("BLOCK_ALL_CARDS", 0.0) == "L2"
    assert route_for("DECLINE_TRANSACTION", 0.0) == "L1"
    for auto in ("CREATE_CASE", "VERIFY_WITH_CUSTOMER", "MONITOR_CONNECTED_CARDS",
                 "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD", "WARN_CUSTOMER", "STEP_UP_AUTH"):
        assert route_for(auto, 10_000.0) == "auto"


# --------------------------------------------------------------------------
# The README worked example
# --------------------------------------------------------------------------

README_EXAMPLE = Assessment(
    verdict="fraud",
    fraud_probability=0.72,
    pattern="card_testing",
    exposure_usd=268.43,
    affected_txn_ids=("T0412877", "T0412878", "T0412879", "T0412883"),
    connected_card_ids=("C00877-K1",),
    connected_device_profiles=("SAMSUNG SM-G892A | Android 7.0 | samsung browser 6.2 | 2220x1080",),
    shared_origin=True,
    shared_origin_label="a device profile shared with card C00877-K1",
    card_testing_sequence=True,
    cleared_purchase_usd=259.98,
    pending_authorization=True,
    independent_signals=2,
)


def test_readme_example_initial_holds_the_block():
    recs = decide(README_EXAMPLE)
    assert "BLOCK_CARD" not in names(recs), "R1: block must be held until the cardholder replies"
    assert "DECLINE_TRANSACTION" in names(recs)
    assert "VERIFY_WITH_CUSTOMER" in names(recs)
    assert routes(recs)["DECLINE_TRANSACTION"] == "L1"
    assert routes(recs)["VERIFY_WITH_CUSTOMER"] == "auto"


def test_readme_example_final_matches_exactly():
    final = decide(README_EXAMPLE.with_response("denied", fraud_probability=0.86))
    assert names(final) == [
        "BLOCK_CARD",
        "CREATE_CASE",
        "FILE_REPORT",
        "MONITOR_CONNECTED_CARDS",
    ]
    assert routes(final) == {
        "BLOCK_CARD": "L1",            # exposure $268 is under $2,500
        "CREATE_CASE": "auto",
        "FILE_REPORT": "L2",
        "MONITOR_CONNECTED_CARDS": "auto",
    }


def test_r5_actions_do_not_persist_after_a_denial():
    """Blocking the card subsumes declining the authorisation."""
    final = decide(README_EXAMPLE.with_response("denied", fraud_probability=0.86))
    assert "DECLINE_TRANSACTION" not in names(final)
    assert "STEP_UP_AUTH" not in names(final)


# --------------------------------------------------------------------------
# R7 -- the disputed-but-legitimate gate
# --------------------------------------------------------------------------

R7_CASE = Assessment(
    verdict="legitimate",
    fraud_probability=0.10,
    pattern="none",
    customer_disputed=True,
    recurring_occurrences=82,      # HHG-003 shape
    independent_signals=2,
)


def test_r7_fires_on_established_recurrence():
    assert recurrence_check(R7_CASE)
    recs = decide(R7_CASE)
    assert names(recs) == ["VERIFY_WITH_CUSTOMER", "CREATE_CASE", "WARN_CUSTOMER"]
    assert "BLOCK_CARD" not in names(recs)


def test_r7_survives_a_denial_without_blocking():
    """A denial is what a dispute *is*. R7 must pre-empt R2, not lose to it."""
    recs = decide(R7_CASE.with_response("denied"))
    assert "BLOCK_CARD" not in names(recs)
    assert "WARN_CUSTOMER" in names(recs)


def test_r7_does_not_fire_on_sparse_recurrence():
    """HHG-009 (4 occurrences) and HHG-016 (2) must not be waved through."""
    for n in (0, 2, 4, 5):
        assert not recurrence_check(Assessment(
            verdict="uncertain", fraud_probability=0.5, pattern="none",
            customer_disputed=True, recurring_occurrences=n,
        ))


def test_r7_needs_a_dispute():
    assert not recurrence_check(Assessment(
        verdict="fraud", fraud_probability=0.9, pattern="none",
        customer_disputed=False, recurring_occurrences=99,
    ))


# --------------------------------------------------------------------------
# SAR -- policy section 3a
# --------------------------------------------------------------------------

def test_sar_on_amount():
    ok, why = should_file_sar(Assessment(
        verdict="fraud", fraud_probability=0.9, pattern="card_not_present_fraud",
        exposure_usd=1_000.01,
    ))
    assert ok and "$1,000" in why


def test_no_sar_at_exactly_the_threshold():
    ok, _ = should_file_sar(Assessment(
        verdict="fraud", fraud_probability=0.9, pattern="card_not_present_fraud",
        exposure_usd=1_000.00,
    ))
    assert not ok, "the policy says 'exceeds $1,000', so $1,000.00 alone does not file"


def test_sar_on_shared_origin_under_threshold():
    """Ring A: exposures of $108-$390 filed because cards were connected."""
    ok, why = should_file_sar(Assessment(
        verdict="fraud", fraud_probability=0.9, pattern="undocumented",
        exposure_usd=390.04, connected_card_ids=("C00255-K1",), shared_origin=True,
    ))
    assert ok and "R6" in why


def test_sar_on_undocumented_alone():
    ok, why = should_file_sar(Assessment(
        verdict="fraud", fraud_probability=0.88, pattern="undocumented", exposure_usd=120.0,
    ))
    assert ok and "R9" in why


def test_no_sar_when_not_suspected():
    ok, _ = should_file_sar(Assessment(
        verdict="legitimate", fraud_probability=0.05, pattern="none", exposure_usd=5_000.0,
    ))
    assert not ok, "a legitimate verdict never files, whatever the amount"


def test_sar_flag_always_agrees_with_the_action_list():
    """The invariant the README calls out, checked across a spread of cases."""
    cases = [
        Assessment(verdict="fraud", fraud_probability=0.9, pattern="card_not_present_fraud",
                   exposure_usd=1_500.0, customer_response="denied", independent_signals=2),
        Assessment(verdict="fraud", fraud_probability=0.9, pattern="undocumented",
                   exposure_usd=300.0, customer_response="denied", independent_signals=2),
        Assessment(verdict="legitimate", fraud_probability=0.05, pattern="none",
                   independent_signals=2),
        R7_CASE,
        README_EXAMPLE.with_response("denied", fraud_probability=0.86),
    ]
    for a in cases:
        expect, _ = should_file_sar(a)
        got = "FILE_REPORT" in names(decide(a))
        if recurrence_check(a):
            expect = False
        assert got == expect, f"SAR mismatch for {a.pattern}/{a.verdict}"


# --------------------------------------------------------------------------
# Ring cases
# --------------------------------------------------------------------------

def test_ring_a_undocumented_files_and_escalates():
    """HHG-014 shape: 28 cards on one device fingerprint."""
    recs = decide(Assessment(
        verdict="fraud", fraud_probability=0.90, pattern="undocumented",
        exposure_usd=327.24, affected_txn_ids=("3478561", "3480221"),
        connected_card_ids=tuple(f"C{i:05d}-K1" for i in range(27)),
        connected_device_profiles=("SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | IP_PROXY:ANONYMOUS",),
        shared_origin=True, shared_origin_label="an anonymised-proxy device profile",
        independent_signals=3,
    ))
    assert {"CREATE_CASE", "FILE_REPORT", "MONITOR_CONNECTED_CARDS", "ESCALATE_TO_ANALYST"} <= set(names(recs))
    assert routes(recs)["FILE_REPORT"] == "L2"


def test_ring_b_files_on_amount_not_connection():
    """HHG-006 shape: $1,906.07 structured under a $500 ceiling, no connected cards."""
    a = Assessment(
        verdict="fraud", fraud_probability=0.88, pattern="undocumented",
        exposure_usd=1_906.07,
        affected_txn_ids=("3476602", "3476633", "3476665", "3476682"),
        customer_disputed=True, recurring_occurrences=0,
        customer_response="denied", independent_signals=3,
    )
    recs = decide(a)
    assert "FILE_REPORT" in names(recs)
    assert routes(recs)["BLOCK_CARD"] == "L1"   # $1,906 is under $2,500
    ok, why = should_file_sar(a)
    assert ok and "$1,906.07" in why


# --------------------------------------------------------------------------
# R8 / R10 / stopping / status
# --------------------------------------------------------------------------

def test_r8_escalates_uncertain_and_exposed():
    recs = decide(Assessment(
        verdict="uncertain", fraud_probability=0.5, pattern="card_not_present_fraud",
        exposure_usd=600.0, independent_signals=2,
    ))
    assert "ESCALATE_TO_ANALYST" in names(recs)


def test_r8_quiet_when_uncertain_but_small():
    recs = decide(Assessment(
        verdict="uncertain", fraud_probability=0.5, pattern="card_not_present_fraud",
        exposure_usd=100.0, independent_signals=2,
    ))
    assert "ESCALATE_TO_ANALYST" not in names(recs)


def test_r10_blocks_all_cards_only_with_grounds():
    base = dict(verdict="fraud", fraud_probability=0.9, pattern="account_takeover",
                exposure_usd=100.0, independent_signals=2, customer_response="denied")
    assert "BLOCK_ALL_CARDS" not in names(decide(Assessment(**base)))
    assert "BLOCK_ALL_CARDS" not in names(
        decide(Assessment(**base, cards_with_confirmed_fraud=1))
    )


def test_stopping_rules():
    high = Assessment(verdict="fraud", fraud_probability=0.9, pattern="x", independent_signals=2)
    assert should_stop(high)[0]
    assert not should_stop(Assessment(
        verdict="fraud", fraud_probability=0.9, pattern="x", independent_signals=1,
    ))[0], "0.85+ still needs two independent pieces of evidence"
    assert should_stop(Assessment(
        verdict="legitimate", fraud_probability=0.10, pattern="none", independent_signals=2,
    ))[0]
    assert should_stop(high.with_response("denied"))[0]


def test_status_derivation():
    fraud = Assessment(verdict="fraud", fraud_probability=0.9, pattern="x", independent_signals=2,
                       customer_response="denied")
    assert status_for(fraud, decide(fraud)) == "closed_fraud"

    legit = Assessment(verdict="legitimate", fraud_probability=0.05, pattern="none",
                       independent_signals=2)
    assert status_for(legit, decide(legit)) == "closed_legitimate"

    unc = Assessment(verdict="uncertain", fraud_probability=0.5, pattern="x",
                     exposure_usd=900.0, independent_signals=2)
    assert status_for(unc, decide(unc)) == "escalated"
    assert status_for(fraud, decide(fraud), evidence_pending=True) == "open"


def test_actions_are_ordered_by_what_happens_first():
    recs = decide(README_EXAMPLE)
    assert names(recs).index("DECLINE_TRANSACTION") < names(recs).index("VERIFY_WITH_CUSTOMER")


def test_every_reason_cites_a_rule():
    for a in (README_EXAMPLE, R7_CASE, README_EXAMPLE.with_response("denied")):
        for r in decide(a):
            assert any(f"R{i}" in r.reason or "3a" in r.reason for i in range(1, 11)), r
