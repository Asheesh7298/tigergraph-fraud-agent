"""The invariants must actually fire. A schema that silently accepts a
contradictory answer file is worse than no schema at all."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from schema import Answer, Case, Evidence, NextBestActions, SAR, check_exposure  # noqa: E402


def _case(**over) -> dict:
    base = dict(
        status="closed_fraud",
        verdict="fraud",
        fraud_probability=0.86,
        pattern="card_not_present_fraud",
        pattern_description="",
        affected_txn_ids=["3476682"],
        first_suspicious_txn_id="3476682",
        connected_card_ids=[],
        connected_device_profiles=[],
        exposure_usd=482.12,
        evidence=[Evidence(claim="c", source="graph", ref="query:card_window", entity_ids=[])],
        similar_prior_cases=[],
        summary="s",
        written_to_graph=True,
        graph_case_id="CASE-2016-0001",
    )
    base.update(over)
    return base


def _answer(**over) -> dict:
    base = dict(
        case_id="HHG-006",
        case=Case(**_case()),
        evidence_requests=[],
        next_best_actions=NextBestActions(
            initial=[{"action": "BLOCK_CARD", "route": "L1", "reason": "R2: denied"}],
            final=[{"action": "BLOCK_CARD", "route": "L1", "reason": "R2: denied"}],
            what_changed="nothing",
        ),
        sar=SAR(file=False, reason="R2: exposure under $1,000 and no shared origin"),
        stop_reason="settled",
        tool_calls=9,
        tokens=1200,
        latency_s=18.7,
    )
    base.update(over)
    return base


def test_valid_answer_builds():
    assert Answer(**_answer()).case_id == "HHG-006"


def test_sar_flag_must_match_final_actions():
    with pytest.raises(ValidationError, match="FILE_REPORT"):
        Answer(**_answer(sar=SAR(
            file=True,
            reason="R2",
            narrative="One. Two. Three. Four. Five. Six. Seven.",
            subjects=["C07297"],
            total_amount_usd=482.12,
            activity_dates=["2016-11-21", "2016-11-21"],
        )))


def test_file_report_without_sar_flag_rejected():
    with pytest.raises(ValidationError, match="FILE_REPORT"):
        Answer(**_answer(next_best_actions=NextBestActions(
            initial=[{"action": "FILE_REPORT", "route": "L2", "reason": "R2"}],
            final=[{"action": "FILE_REPORT", "route": "L2", "reason": "R2"}],
            what_changed="nothing",
        )))


def test_legitimate_cannot_name_affected_txns():
    with pytest.raises(ValidationError, match="legitimate"):
        Case(**_case(verdict="legitimate", pattern="none"))


def test_legitimate_must_have_zero_exposure():
    with pytest.raises(ValidationError, match="legitimate"):
        Case(**_case(
            verdict="legitimate", pattern="none",
            affected_txn_ids=[], first_suspicious_txn_id="", exposure_usd=10.0,
        ))


def test_undocumented_requires_description():
    with pytest.raises(ValidationError, match="pattern_description"):
        Case(**_case(pattern="undocumented", pattern_description=""))


def test_description_only_for_undocumented():
    with pytest.raises(ValidationError, match="pattern_description"):
        Case(**_case(pattern="card_testing", pattern_description="x"))


def test_first_suspicious_must_be_in_affected():
    with pytest.raises(ValidationError, match="first_suspicious"):
        Case(**_case(first_suspicious_txn_id="9999999"))


def test_written_to_graph_requires_id():
    with pytest.raises(ValidationError, match="graph_case_id"):
        Case(**_case(written_to_graph=True, graph_case_id=""))


def test_action_reason_must_cite_a_rule():
    with pytest.raises(ValidationError, match="R1..R10"):
        NextBestActions(
            initial=[{"action": "BLOCK_CARD", "route": "L1", "reason": "seemed bad"}],
            final=[{"action": "BLOCK_CARD", "route": "L1", "reason": "seemed bad"}],
            what_changed="nothing",
        )


def test_what_changed_must_say_nothing_when_identical():
    with pytest.raises(ValidationError, match="what_changed"):
        NextBestActions(
            initial=[{"action": "BLOCK_CARD", "route": "L1", "reason": "R2"}],
            final=[{"action": "BLOCK_CARD", "route": "L1", "reason": "R2"}],
            what_changed="customer denied",
        )


def test_what_changed_cannot_be_nothing_when_different():
    with pytest.raises(ValidationError, match="what_changed"):
        NextBestActions(
            initial=[{"action": "VERIFY_WITH_CUSTOMER", "route": "auto", "reason": "R1"}],
            final=[{"action": "BLOCK_CARD", "route": "L1", "reason": "R2"}],
            what_changed="nothing",
        )


def test_sar_false_must_be_empty():
    with pytest.raises(ValidationError, match="narrative/subjects/dates"):
        SAR(file=False, reason="R2", narrative="x", subjects=["C1"])


def test_sar_true_needs_six_sentences():
    with pytest.raises(ValidationError, match="six to twelve"):
        SAR(file=True, reason="R2", narrative="Too short.", subjects=["C1"],
            total_amount_usd=5.0, activity_dates=["2016-11-21", "2016-11-21"])


def test_customer_evidence_requires_a_request():
    with pytest.raises(ValidationError, match="evidence_request"):
        Answer(**_answer(case=Case(**_case(evidence=[
            Evidence(claim="denied it", source="customer", ref="evidence_request:1", entity_ids=[]),
        ]))))


def test_exposure_must_match_affected_amounts():
    a = Answer(**_answer(case=Case(**_case(
        affected_txn_ids=["3476602", "3476633", "3476665", "3476682"],
        first_suspicious_txn_id="3476602",
        exposure_usd=1906.07,
    ))))
    amounts = {"3476602": 478.95, "3476633": 456.96, "3476665": 488.04, "3476682": 482.12}
    assert check_exposure(a, amounts) == []

    bad = Answer(**_answer(case=Case(**_case(exposure_usd=999.99))))
    assert check_exposure(bad, {"3476682": 482.12})
