"""Episode expansion and pattern classification -- in code, not in the model.

Both models tested named the Ring B burst correctly in prose ("threshold
structuring, four charges of $450-499 inside thirty minutes") and then
labelled it `card_not_present_fraud` and `card_testing` respectively. Neither
reached `undocumented`. Reasoning about evidence and selecting a label from a
closed vocabulary are different skills, and only one of them needs a model.

So the classifier is rules, the LLM confirms and explains, and when they
disagree the disagreement is recorded rather than silently resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

TS = "%Y-%m-%d %H:%M:%S"

# Ring B, measured from CC-3748 / CC-3841 / CC-3907 / CC-4086 / CC-4124
RING_B_LEGS = 4
RING_B_MIN, RING_B_MAX = 450.0, 500.0
RING_B_WINDOW_MIN = 45
RING_B_TOTAL_MIN, RING_B_TOTAL_MAX = 1_800.0, 2_000.0

# R5 card testing
TESTING_SMALL = 5.0
TESTING_MIN_LEGS = 3
TESTING_WINDOW_MIN = 60

# Episode expansion
BURST_HOURS = 2.0
MAX_EPISODE = 400          # a guard, not a cap -- see note in expand_episode


def as_dt(v: str | datetime) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.strptime(str(v).strip().replace("T", " ")[:19], TS)


@dataclass
class Signal:
    """One independent reason to suspect, recorded so it can be counted."""

    name: str
    detail: str
    entity_ids: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    pattern: str = "none"
    description: str = ""
    signals: list[Signal] = field(default_factory=list)
    ring_profile: str = ""
    connected_cards: list[str] = field(default_factory=list)

    @property
    def independent_signals(self) -> int:
        return len({s.name for s in self.signals})


# --------------------------------------------------------------------------
# Episode expansion
# --------------------------------------------------------------------------

def expand_episode(
    flagged: dict[str, Any],
    window: list[dict[str, Any]],
    baseline: dict[str, Any],
    ring_txn_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Which transactions belong to the same fraud episode.

    Walks outward from the flagged transaction in both directions and keeps
    going while the next transaction shares a linking feature. Walking
    backwards matters: the flagged transaction is frequently not the first.
    On HHG-014 it is the middle of three, and on HHG-006 it is the last of
    four -- taking the flagged transaction as the start understates the
    episode by most of its value in both.

    There is deliberately no fixed cap. Capping every episode at four
    transactions would have missed 36.8% of the fraudulent transactions in
    the closed history: the median confirmed episode is 1-2, but
    account_takeover reaches 211 and card_not_present_new_device reaches 356.
    The stopping condition is a shared feature, not a count.
    """
    if not window:
        return [flagged]

    ring_txn_ids = ring_txn_ids or set()
    ordered = sorted(window, key=lambda t: t["ts"])
    ids = [t["txn_id"] for t in ordered]
    if flagged["txn_id"] not in ids:
        ordered.append(flagged)
        ordered.sort(key=lambda t: t["ts"])
        ids = [t["txn_id"] for t in ordered]
    pivot = ids.index(flagged["txn_id"])

    usual_regions = set((baseline.get("regions") or {}).keys())
    usual_products = set((baseline.get("products") or {}).keys())
    usual_devices = set((baseline.get("devices") or {}).keys())
    f_ts = as_dt(flagged["ts"])

    def links(t: dict[str, Any]) -> str | None:
        """Why this transaction belongs with the flagged one, if it does."""
        if t["txn_id"] in ring_txn_ids:
            return "same ring device profile"
        dev = t.get("device_profile") or ""
        if dev and dev == (flagged.get("device_profile") or ""):
            return "same device profile"
        if dev and dev not in usual_devices and usual_devices:
            return "device unseen on this card"
        reg = t.get("addr1") or ""
        if reg and reg not in usual_regions and usual_regions:
            return "billing region unseen on this card"
        prod = t.get("product_cd") or ""
        if prod and prod not in usual_products and usual_products:
            return "product code unseen on this card"
        # Time proximity alone is too loose: an ordinary in-person purchase
        # that happens to fall within two hours of an online fraud is not part
        # of it. HHG-014's episode was pulling in a $31 in-person W charge next
        # to the online ring purchases. Require the same channel as the flagged
        # transaction, so an online burst stays online.
        if (
            t.get("channel") == flagged.get("channel")
            and abs((as_dt(t["ts"]) - f_ts).total_seconds()) <= BURST_HOURS * 3600
        ):
            return "inside the burst window"
        return None

    episode = [ordered[pivot]]
    taken = {ordered[pivot]["txn_id"]}

    # Ring membership is a direct link, not a proximity one, so it is not
    # subject to the contiguous walk. A ring spreads thinly: HHG-014's three
    # legs sit 7 and 4 days apart with ordinary in-person spend in between,
    # and a walk that stops at the first unrelated transaction would find
    # only the flagged one.
    for t in ordered:
        if t["txn_id"] in ring_txn_ids and t["txn_id"] not in taken:
            episode.append({**t, "_link": "same ring device profile"})
            taken.add(t["txn_id"])

    # Then walk outward for a contiguous burst, which is how the structuring
    # and card-testing shapes present.
    for direction in (-1, 1):
        i = pivot + direction
        while 0 <= i < len(ordered) and len(episode) < MAX_EPISODE:
            if ordered[i]["txn_id"] in taken:
                i += direction
                continue
            why = links(ordered[i])
            if why is None:
                break
            episode.append({**ordered[i], "_link": why})
            taken.add(ordered[i]["txn_id"])
            i += direction

    return sorted(episode, key=lambda t: t["ts"])


# --------------------------------------------------------------------------
# Pattern classification -- first match wins
# --------------------------------------------------------------------------

def classify(
    flagged: dict[str, Any],
    episode: list[dict[str, Any]],
    baseline: dict[str, Any],
    ring: dict[str, Any] | None = None,
    region: dict[str, Any] | None = None,
    card_id: str = "",
) -> Assessment:
    """Order matters: rings are tested before card testing.

    A four-leg structuring burst tested against card testing first would be
    filed as a testing run, and card testing is 0.34% of confirmed history
    (16 of 4,665) while its median episode is 21 transactions -- so it is
    both the rarest pattern and the easiest to over-predict, because the
    README's one worked example happens to be card testing.
    """
    a = Assessment()
    # The subject's own card is not one of its "connected" cards. Taken from
    # the flagged transaction only as a fallback, so a caller that knows the
    # card never depends on the transaction dict carrying it.
    subject_card = card_id or flagged.get("card_id") or ""
    amounts = [float(t["amount"]) for t in episode]
    online = [t for t in episode if t.get("channel") == "online"]
    in_person = [t for t in episode if t.get("channel") == "in_person"]
    span_min = _span_minutes(episode)

    # 1. Ring A -- a device new to every account it touches, behind a proxy,
    #    shared across several cards.
    if ring and ring.get("n_cards", 0) >= 3:
        a.pattern = "undocumented"
        a.ring_profile = ring.get("profile", "")
        a.connected_cards = [c for c in (ring.get("cards") or []) if c != subject_card]
        a.description = (
            f"A single device profile ({a.ring_profile}) was used on "
            f"{ring.get('n_cards')} different cards between "
            f"{ring.get('first_seen', '')[:10]} and {ring.get('last_seen', '')[:10]}. "
            f"It was recorded as new to every account it touched and sat behind an "
            f"anonymising proxy on every transaction, which is not how a genuinely "
            f"shared device behaves -- a household or office device shows returning "
            f"users. The purchases are small and spread thinly across many cards, so "
            f"no single cardholder looks alarming; the pattern is only visible as a "
            f"shared-device cluster across the portfolio."
        )
        a.signals.append(Signal(
            "shared_ring_device",
            f"device profile shared with {ring.get('n_cards')} cards, "
            f"new to every account, anonymised on every transaction",
            [a.ring_profile],
        ))
        if len(episode) > 1:
            a.signals.append(Signal(
                "repeat_use", f"{len(episode)} transactions on this card from the same profile",
                [t["txn_id"] for t in episode],
            ))
        return a

    # 2. Ring B -- threshold structuring under an authorisation ceiling.
    if (
        len(episode) == RING_B_LEGS
        and len(online) == RING_B_LEGS
        and all(RING_B_MIN <= x < RING_B_MAX for x in amounts)
        and span_min <= RING_B_WINDOW_MIN
        and RING_B_TOTAL_MIN <= sum(amounts) <= RING_B_TOTAL_MAX
    ):
        a.pattern = "undocumented"
        a.description = (
            f"Four online purchases of ${amounts[0]:,.2f}, ${amounts[1]:,.2f}, "
            f"${amounts[2]:,.2f} and ${amounts[3]:,.2f} were made on this card within "
            f"{span_min:.0f} minutes, totalling ${sum(amounts):,.2f}. Every leg sits "
            f"just below $500, which reads as deliberate structuring under a "
            f"per-transaction authorisation ceiling rather than as shopping: the card "
            f"is drained in one sitting while no single charge is large enough to "
            f"require extra approval. It matches none of the five documented patterns "
            f"and leaves no shared-device trail."
        )
        a.signals.append(Signal(
            "threshold_structuring",
            f"{RING_B_LEGS} legs of $450-499 in {span_min:.0f} minutes, "
            f"total ${sum(amounts):,.2f}",
            [t["txn_id"] for t in episode],
        ))
        a.signals.append(Signal(
            "off_baseline_amount",
            f"each leg is far above this card's usual ${baseline.get('p50', 0):,.2f}",
            [t["txn_id"] for t in episode],
        ))
        return a

    # 3. Card testing (R5).
    small = [t for t in online if float(t["amount"]) < TESTING_SMALL]
    if len(small) >= TESTING_MIN_LEGS and _span_minutes(small) <= TESTING_WINDOW_MIN:
        larger = [t for t in online
                  if float(t["amount"]) >= TESTING_SMALL
                  and as_dt(t["ts"]) > as_dt(small[0]["ts"])]
        if larger:
            a.pattern = "card_testing"
            a.signals.append(Signal(
                "testing_sequence",
                f"{len(small)} online authorisations under ${TESTING_SMALL:.0f} within "
                f"{_span_minutes(small):.0f} minutes, then a larger purchase",
                [t["txn_id"] for t in small + larger[:1]],
            ))
            return a

    # 4. Out-of-region card-present use, with home activity continuing.
    if region and in_person:
        unseen = int(region.get("n_in_region", 0)) == 0
        parallel = int(region.get("parallel_elsewhere", 0)) > 0
        if unseen and parallel:
            a.pattern = "out_of_region_use"
            a.signals.append(Signal(
                "unseen_region",
                f"card-present use in billing region {flagged.get('addr1')}, "
                f"which this card has never used in {region.get('n_total')} prior transactions",
                [flagged["txn_id"]],
            ))
            a.signals.append(Signal(
                "parallel_home_activity",
                f"{region.get('parallel_elsewhere')} transactions elsewhere within days, "
                f"so the cardholder was not simply travelling",
                list(region.get("parallel_txn_ids") or [])[:6],
            ))
            return a

    # 5. Account takeover -- both channels inside one episode, with match-flag
    #    anomalies. M1-M9 are populated almost only on in-person rows, so
    #    their absence on an online row is structural and is not a signal.
    if online and in_person:
        odd = _match_flag_anomalies(episode)
        a.pattern = "account_takeover"
        a.signals.append(Signal(
            "mixed_channel",
            f"{len(in_person)} in-person and {len(online)} online transactions inside "
            f"one episode, which is inconsistent with a single cardholder",
            [t["txn_id"] for t in episode][:8],
        ))
        if odd:
            a.signals.append(Signal(
                "match_flag_anomaly",
                f"match flags differ from this card's norm on {len(odd)} transaction(s)",
                odd[:6],
            ))
        return a

    # 6/7. Card-not-present, with or without a new device.
    if online and not in_person:
        new_device = any(t.get("device_new") == "New" for t in episode)
        unusual = _off_baseline(episode, baseline)
        if new_device:
            a.pattern = "card_not_present_new_device"
            a.signals.append(Signal(
                "new_device",
                "the device was recorded as new to this account",
                [t["txn_id"] for t in episode if t.get("device_new") == "New"][:6],
            ))
        else:
            a.pattern = "card_not_present_fraud"
        if unusual:
            a.signals.append(Signal("off_baseline", unusual, [t["txn_id"] for t in episode][:6]))
        if a.signals:
            return a

    # 8. Nothing that distinguishes it from how this card is normally used.
    a.pattern = "none"
    return a


# --------------------------------------------------------------------------

def _span_minutes(txns: list[dict[str, Any]]) -> float:
    if len(txns) < 2:
        return 0.0
    stamps = sorted(as_dt(t["ts"]) for t in txns)
    return (stamps[-1] - stamps[0]).total_seconds() / 60.0


def _match_flag_anomalies(episode: list[dict[str, Any]]) -> list[str]:
    """Transactions whose M4 differs from the rest of the episode.

    Only M4 is consulted: M1-M3 and M5-M9 are populated almost exclusively on
    ProductCD=W rows, so comparing them across a mixed-channel episode
    compares presence, not values.
    """
    values = [t.get("M4") for t in episode if t.get("M4")]
    if len(set(values)) <= 1:
        return []
    common = max(set(values), key=values.count)
    return [t["txn_id"] for t in episode if t.get("M4") and t["M4"] != common]


def _off_baseline(episode: list[dict[str, Any]], baseline: dict[str, Any]) -> str:
    p90 = float(baseline.get("p90") or 0)
    mx = float(baseline.get("max") or 0)
    products = set((baseline.get("products") or {}).keys())
    reasons: list[str] = []

    big = [t for t in episode if mx and float(t["amount"]) > mx]
    if big:
        reasons.append(
            f"${max(float(t['amount']) for t in big):,.2f} exceeds this card's "
            f"highest prior transaction of ${mx:,.2f}"
        )
    elif p90 and any(float(t["amount"]) > p90 for t in episode):
        reasons.append(f"above this card's 90th-percentile amount of ${p90:,.2f}")

    novel = {t.get("product_cd") for t in episode if t.get("product_cd") not in products}
    novel.discard(None)
    novel.discard("")
    if novel and products:
        reasons.append(f"product code {'/'.join(sorted(novel))} never used on this card before")

    return "; ".join(reasons)
