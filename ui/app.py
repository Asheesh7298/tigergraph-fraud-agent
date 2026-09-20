"""Read-only analyst viewer over the twenty answer files.

The UI is 10% of the score, and a read-only viewer that presents the case,
the evidence, the initial-versus-final recommendation and the SAR is worth
far more per hour than a live dashboard. It reads cases/*.json -- the same
files that are graded -- so it can never drift from what was submitted.

    streamlit run ui/app.py
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "cases"

VERDICT_COLOR = {"fraud": "#c0392b", "legitimate": "#27ae60", "uncertain": "#f39c12"}
ROUTE_COLOR = {"auto": "#7f8c8d", "L1": "#d68910", "L2": "#c0392b"}

st.set_page_config(page_title="Fraud Investigation Cases", layout="wide")


@st.cache_data
def load_cases() -> list[dict]:
    out = []
    for p in sorted(CASES.glob("HHG-*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    return out


def chip(text: str, color: str) -> str:
    return (
        f"<span style='background:{color};color:white;padding:2px 10px;"
        f"border-radius:12px;font-size:0.8em;font-weight:600'>{text}</span>"
    )


def action_row(a: dict) -> str:
    return (
        f"<div style='margin:3px 0'>"
        f"{chip(a['route'], ROUTE_COLOR.get(a['route'], '#7f8c8d'))} "
        f"<b>{a['action']}</b> "
        f"<span style='color:#666;font-size:0.9em'>&mdash; {a['reason']}</span></div>"
    )


cases = load_cases()
if not cases:
    st.warning("No answer files found in cases/. Run: python src/run_cases.py --all")
    st.stop()

# -- sidebar: portfolio overview -------------------------------------------
st.sidebar.title("Case pack")
verdicts = {}
for c in cases:
    verdicts[c["case"]["verdict"]] = verdicts.get(c["case"]["verdict"], 0) + 1
st.sidebar.caption(f"{len(cases)} cases")
cols = st.sidebar.columns(3)
cols[0].metric("Fraud", verdicts.get("fraud", 0))
cols[1].metric("Legit", verdicts.get("legitimate", 0))
cols[2].metric("Uncertain", verdicts.get("uncertain", 0))
sars = sum(1 for c in cases if c["sar"]["file"])
st.sidebar.metric("Reports filed", sars)

labels = [
    f"{c['case_id']}  ·  {c['case']['verdict']}  ·  {c['case']['pattern']}"
    for c in cases
]
idx = st.sidebar.radio("Open case", range(len(cases)), format_func=lambda i: labels[i])
case = cases[idx]
k = case["case"]

# -- header ----------------------------------------------------------------
st.markdown(
    f"## {case['case_id']} &nbsp; "
    + chip(k["verdict"].upper(), VERDICT_COLOR.get(k["verdict"], "#333")),
    unsafe_allow_html=True,
)
top = st.columns(5)
top[0].metric("Pattern", k["pattern"])
top[1].metric("Fraud probability", f"{k['fraud_probability']:.2f}")
top[2].metric("Exposure", f"${k['exposure_usd']:,.2f}")
top[3].metric("Status", k["status"])
top[4].metric("Report", "yes" if case["sar"]["file"] else "no")

st.write(k["summary"])
if k["pattern"] == "undocumented" and k.get("pattern_description"):
    st.info(k["pattern_description"])

# -- next best action: initial vs final ------------------------------------
st.markdown("### Next best action")
c1, c2 = st.columns(2)
with c1:
    st.caption("INITIAL — before requesting evidence")
    st.markdown("".join(action_row(a) for a in case["next_best_actions"]["initial"]),
                unsafe_allow_html=True)
with c2:
    st.caption("FINAL — after the assumed response")
    st.markdown("".join(action_row(a) for a in case["next_best_actions"]["final"]),
                unsafe_allow_html=True)
if case["next_best_actions"]["what_changed"].strip().lower() != "nothing":
    st.caption(f"What changed: {case['next_best_actions']['what_changed']}")

if case["evidence_requests"]:
    with st.expander(f"Evidence requested ({len(case['evidence_requests'])})"):
        for r in case["evidence_requests"]:
            st.markdown(f"**{r['type']}** (after step {r['asked_after_step']}): "
                        f"_{r['assumed_response']}_")

# -- evidence --------------------------------------------------------------
st.markdown("### Evidence")
for e in k["evidence"]:
    st.markdown(
        f"- {chip(e['source'], '#34495e')} {e['claim']}  \n"
        f"  <span style='color:#888;font-size:0.85em'>{e['ref']}"
        + (f" · {', '.join(e['entity_ids'][:8])}" if e["entity_ids"] else "")
        + "</span>",
        unsafe_allow_html=True,
    )

# -- affected transactions and connections ---------------------------------
c1, c2 = st.columns(2)
with c1:
    if k["affected_txn_ids"]:
        st.markdown(f"**Affected transactions ({len(k['affected_txn_ids'])})**")
        st.caption(f"first suspicious: {k['first_suspicious_txn_id']}")
        st.code("\n".join(k["affected_txn_ids"]))
with c2:
    if k["connected_card_ids"]:
        st.markdown(f"**Connected cards ({len(k['connected_card_ids'])})**")
        st.code("\n".join(k["connected_card_ids"]))
    if k["connected_device_profiles"]:
        st.markdown("**Device profile**")
        st.code("\n".join(k["connected_device_profiles"]))

if k["similar_prior_cases"]:
    st.markdown("**Prior cases used as memory:** " + ", ".join(k["similar_prior_cases"]))

# -- SAR -------------------------------------------------------------------
st.markdown("### Suspicious activity report")
if case["sar"]["file"]:
    s = case["sar"]
    st.caption(f"{s['reason']}  ·  ${s['total_amount_usd']:,.2f}  ·  "
               f"{' to '.join(s['activity_dates'])}")
    st.write(s["narrative"])
    st.caption("Subjects: " + ", ".join(s["subjects"]))
else:
    st.caption(f"No report filed. {case['sar']['reason']}")

# -- footer ----------------------------------------------------------------
st.divider()
f = st.columns(4)
f[0].caption(f"tool calls: {case['tool_calls']}")
f[1].caption(f"tokens: {case['tokens']:,}")
f[2].caption(f"latency: {case['latency_s']}s")
f[3].caption(f"written to graph: {k['written_to_graph']} ({k.get('graph_case_id') or '—'})")
st.caption(f"stop reason: {case['stop_reason']}")
