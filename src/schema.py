"""Answer-file models for the 20 case files, with the invariants enforced.

Every rule in CLAUDE.md 6.6 is checked at construction. An answer that
contradicts itself -- a SAR flagged without FILE_REPORT in the final actions,
an exposure that does not match the affected transactions, a legitimate
verdict that still names affected transactions -- cannot be built.

The graders read these files directly; roughly 60% of the score is in them.
Catching an inconsistency here costs a traceback. Catching it after
submission costs the marks.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------
# Vocabulary -- these strings are fixed by the policy. Do not invent values.
# --------------------------------------------------------------------------

ACTIONS = (
    "ALLOW_TRANSACTION",
    "DECLINE_TRANSACTION",
    "MONITOR_CARD",
    "MONITOR_CONNECTED_CARDS",
    "WARN_CUSTOMER",
    "VERIFY_WITH_CUSTOMER",
    "STEP_UP_AUTH",
    "BLOCK_CARD",
    "BLOCK_ALL_CARDS",
    "GENERATE_REPORT",
    "CREATE_CASE",
    "FILE_REPORT",
    "ESCALATE_TO_ANALYST",
    "CLOSE_NO_FRAUD",
)

PATTERNS = (
    "card_testing",
    "card_not_present_fraud",
    "card_not_present_new_device",
    "out_of_region_use",
    "account_takeover",
    "undocumented",
    "none",
)

#: Policy section 7 requires every recommendation to cite the rule it follows
#: from. Most map to a numbered rule, but two do not: section 3a governs when
#: a case is opened and when a report is filed, and section 6 governs
#: stopping. Citing those sections is honest; inventing an R-number to satisfy
#: a checker would not be.
RULE_TOKENS = tuple(f"R{i}" for i in range(1, 11)) + ("3a", "3b", "section 6")

Action = Literal[ACTIONS]  # type: ignore[valid-type]
Route = Literal["auto", "L1", "L2"]
Pattern = Literal[PATTERNS]  # type: ignore[valid-type]
Verdict = Literal["fraud", "legitimate", "uncertain"]
Status = Literal["open", "closed_fraud", "closed_legitimate", "escalated"]
Source = Literal["graph", "document", "customer", "external"]
RequestType = Literal["customer_validation", "step_up_auth", "analyst_info"]

CENTS = 0.011  # float tolerance for money comparisons


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------
# Leaves
# --------------------------------------------------------------------------

class Evidence(Strict):
    claim: str = Field(min_length=1)
    source: Source
    ref: str = Field(min_length=1)
    entity_ids: list[str] = Field(default_factory=list)


class EvidenceRequest(Strict):
    type: RequestType
    asked_after_step: int = Field(ge=0)
    assumed_response: str = Field(min_length=1)


class ActionRec(Strict):
    action: Action
    route: Route
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def reason_cites_a_rule(self) -> "ActionRec":
        # Policy section 7: every recommendation cites the rule it follows from.
        if not any(tok in self.reason for tok in RULE_TOKENS):
            raise ValueError(
                f"action {self.action}: reason must cite a policy rule R1..R10, got {self.reason!r}"
            )
        return self


class NextBestActions(Strict):
    initial: list[ActionRec] = Field(min_length=1)
    final: list[ActionRec] = Field(min_length=1)
    what_changed: str = Field(min_length=1)

    @model_validator(mode="after")
    def changed_iff_different(self) -> "NextBestActions":
        same = [(a.action, a.route) for a in self.initial] == [
            (a.action, a.route) for a in self.final
        ]
        says_nothing = self.what_changed.strip().lower() == "nothing"
        if same and not says_nothing:
            raise ValueError("initial == final but what_changed is not 'nothing'")
        if not same and says_nothing:
            raise ValueError("initial != final but what_changed is 'nothing'")
        return self


class SAR(Strict):
    file: bool
    reason: str = Field(min_length=1)
    narrative: str = ""
    subjects: list[str] = Field(default_factory=list)
    total_amount_usd: float = 0.0
    activity_dates: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_with_file_flag(self) -> "SAR":
        if self.file:
            if not self.narrative.strip():
                raise ValueError("sar.file is true but narrative is empty")
            # Count sentence endings rather than periods: amounts like
            # "$1,906.07" and ids like "CC-0141" carry dots of their own, so
            # splitting on "." both over- and under-counts.
            sentences = len(re.findall(r"[.!?](?:\s|$)", self.narrative))
            if sentences < 5:
                raise ValueError(
                    f"sar.narrative must be six to twelve sentences, found {sentences}"
                )
            if not self.subjects:
                raise ValueError("sar.file is true but subjects is empty")
            if self.total_amount_usd <= 0:
                raise ValueError("sar.file is true but total_amount_usd is not positive")
            if len(self.activity_dates) != 2:
                raise ValueError("sar.activity_dates must hold exactly two dates")
        else:
            if self.narrative or self.subjects or self.activity_dates:
                raise ValueError("sar.file is false: narrative/subjects/dates must be empty")
            if self.total_amount_usd != 0:
                raise ValueError("sar.file is false: total_amount_usd must be 0")
        return self


# --------------------------------------------------------------------------
# The case
# --------------------------------------------------------------------------

class Case(Strict):
    status: Status
    verdict: Verdict
    fraud_probability: float = Field(ge=0.0, le=1.0)
    pattern: Pattern
    pattern_description: str = ""
    affected_txn_ids: list[str] = Field(default_factory=list)
    first_suspicious_txn_id: str = ""
    connected_card_ids: list[str] = Field(default_factory=list)
    connected_device_profiles: list[str] = Field(default_factory=list)
    exposure_usd: float = Field(default=0.0, ge=0.0)
    evidence: list[Evidence] = Field(default_factory=list, min_length=1)
    similar_prior_cases: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    written_to_graph: bool = False
    graph_case_id: str = ""

    @model_validator(mode="after")
    def invariants(self) -> "Case":
        if self.pattern == "undocumented" and not self.pattern_description.strip():
            raise ValueError("pattern 'undocumented' requires a pattern_description")
        if self.pattern != "undocumented" and self.pattern_description.strip():
            raise ValueError("pattern_description is only for pattern 'undocumented'")

        if self.verdict == "legitimate":
            if self.affected_txn_ids:
                raise ValueError("verdict 'legitimate' must have no affected_txn_ids")
            if self.exposure_usd != 0:
                raise ValueError("verdict 'legitimate' must have exposure_usd 0")
            if self.pattern != "none":
                raise ValueError("verdict 'legitimate' must have pattern 'none'")
        if self.verdict == "fraud" and not self.affected_txn_ids:
            raise ValueError("verdict 'fraud' must name at least one affected transaction")

        if self.affected_txn_ids and not self.first_suspicious_txn_id:
            raise ValueError("affected_txn_ids is non-empty but first_suspicious_txn_id is blank")
        if self.first_suspicious_txn_id and self.first_suspicious_txn_id not in self.affected_txn_ids:
            raise ValueError("first_suspicious_txn_id must be one of affected_txn_ids")
        if len(set(self.affected_txn_ids)) != len(self.affected_txn_ids):
            raise ValueError("affected_txn_ids contains duplicates")

        if self.written_to_graph and not self.graph_case_id:
            raise ValueError("written_to_graph is true but graph_case_id is blank")
        return self


class Answer(Strict):
    case_id: str = Field(min_length=1)
    case: Case
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list)
    next_best_actions: NextBestActions
    sar: SAR
    stop_reason: str = Field(min_length=1)
    tool_calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    latency_s: float = Field(ge=0.0)

    @model_validator(mode="after")
    def cross_part_invariants(self) -> "Answer":
        # The README calls this one out explicitly.
        files_report = any(a.action == "FILE_REPORT" for a in self.next_best_actions.final)
        if self.sar.file != files_report:
            raise ValueError(
                f"sar.file is {self.sar.file} but FILE_REPORT "
                f"{'is' if files_report else 'is not'} in final actions"
            )
        if self.sar.file and abs(self.sar.total_amount_usd - self.case.exposure_usd) > CENTS:
            raise ValueError(
                f"sar.total_amount_usd {self.sar.total_amount_usd} != "
                f"case.exposure_usd {self.case.exposure_usd}"
            )
        if self.case.verdict == "legitimate" and self.sar.file:
            raise ValueError("verdict 'legitimate' cannot file a report")

        # An evidence request must be reflected in the final recommendation.
        if not self.evidence_requests and self.next_best_actions.what_changed.strip().lower() != "nothing":
            raise ValueError("no evidence was requested, so final must equal initial")

        # Evidence sourced from the customer implies we actually asked.
        if any(e.source == "customer" for e in self.case.evidence) and not self.evidence_requests:
            raise ValueError("evidence cites source 'customer' but no evidence_request was made")
        return self

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def read(cls, path: Path) -> "Answer":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Exposure + ID validation against the real dataset
# --------------------------------------------------------------------------

def check_exposure(answer: Answer, amount_of: dict[str, float]) -> list[str]:
    """Exposure must equal the summed absolute amounts of affected txns."""
    missing = [t for t in answer.case.affected_txn_ids if t not in amount_of]
    if missing:
        return [f"{answer.case_id}: unknown transaction ids {missing}"]
    want = round(sum(abs(amount_of[t]) for t in answer.case.affected_txn_ids), 2)
    if not math.isclose(want, answer.case.exposure_usd, abs_tol=CENTS):
        return [
            f"{answer.case_id}: exposure_usd {answer.case.exposure_usd} != "
            f"sum of affected amounts {want}"
        ]
    return []


class IdRegistry:
    """Every ID in an answer file must exist in the dataset. Made-up IDs score zero."""

    def __init__(
        self,
        txn_ids: set[str],
        card_ids: set[str],
        customer_ids: set[str],
        closed_case_ids: set[str],
        device_profiles: set[str],
        case_ids: set[str] | None = None,
    ) -> None:
        self.txn_ids = txn_ids
        self.card_ids = card_ids
        self.customer_ids = customer_ids
        self.closed_case_ids = closed_case_ids
        self.device_profiles = device_profiles
        # The 20 HHG exam case ids. They exist in case_pack.csv, so citing an
        # earlier one as case memory is legitimate; they are kept apart from
        # closed_case_ids so similar_prior_cases still admits only CC-* cases.
        self.case_ids = case_ids or set()

    @classmethod
    def from_disk(cls, data_dir: Path) -> "IdRegistry":
        payload = json.loads((data_dir / "id_registry.json").read_text(encoding="utf-8"))
        return cls(
            txn_ids=set(payload["txn_ids"]),
            card_ids=set(payload["card_ids"]),
            customer_ids=set(payload["customer_ids"]),
            closed_case_ids=set(payload["closed_case_ids"]),
            device_profiles=set(payload["device_profiles"]),
            case_ids=set(payload.get("case_ids") or []),
        )

    def save(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "id_registry.json").write_text(
            json.dumps(
                {
                    "txn_ids": sorted(self.txn_ids),
                    "card_ids": sorted(self.card_ids),
                    "customer_ids": sorted(self.customer_ids),
                    "closed_case_ids": sorted(self.closed_case_ids),
                    "device_profiles": sorted(self.device_profiles),
                    "case_ids": sorted(self.case_ids),
                }
            ),
            encoding="utf-8",
        )

    def check(self, answer: Answer) -> list[str]:
        p: list[str] = []
        c = answer.case

        def unknown(kind: str, ids: Iterable[str], known: set[str]) -> None:
            bad = [i for i in ids if i and i not in known]
            if bad:
                p.append(f"{answer.case_id}: {kind} not in dataset: {bad}")

        unknown("transaction id", c.affected_txn_ids, self.txn_ids)
        unknown("first_suspicious_txn_id", [c.first_suspicious_txn_id], self.txn_ids)
        unknown("connected card id", c.connected_card_ids, self.card_ids)
        unknown("similar_prior_case", c.similar_prior_cases, self.closed_case_ids)
        unknown("device profile", c.connected_device_profiles, self.device_profiles)

        # SAR subjects mix customers, cards and devices.
        allowed = self.customer_ids | self.card_ids | self.device_profiles | self.txn_ids
        unknown("sar subject", answer.sar.subjects, allowed)

        # Evidence entity_ids may reference anything real, including a prior
        # HHG case cited as memory and the agent's own graph case ids.
        every = allowed | self.closed_case_ids | self.case_ids
        for e in c.evidence:
            ids = [i for i in e.entity_ids if not i.startswith("CASE-2016-")]
            unknown(f"evidence entity ({e.ref})", ids, every)
        return p
