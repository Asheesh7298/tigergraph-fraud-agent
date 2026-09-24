import { useMemo, useState } from "react";
import casesData from "./data/cases.json";
import CaseGraph from "./CaseGraph";

const VERDICT = { fraud: "#e5484d", legitimate: "#30a46c", uncertain: "#f5a623" };
const VERDICT_LABEL = { fraud: "Fraud", legitimate: "Legitimate", uncertain: "Ambiguous" };
const ROUTE = { auto: "#8b8b8b", L1: "#d9820b", L2: "#e5484d" };
const SOURCE = { graph: "#4c6ef5", document: "#7048e8", customer: "#0ca678", external: "#e8892b" };
const TRIGGER_LABEL = {
  risk_score: "Signal",
  customer_report: "Customer Report",
  analyst_request: "Analyst",
};

/* ---------- data helpers: every value derived from the real answer file ---------- */
function findCardId(c) {
  for (const e of c.case.evidence || []) {
    const m = (e.ref || "").match(/card_id=([A-Za-z0-9-]+)/);
    if (m) return m[1];
  }
  const t = (c.trigger_text || "").match(/C\d+-K\d+/);
  return t ? t[0] : c.case.connected_card_ids[0] || "—";
}
function flaggedAmount(c) {
  const m = (c.trigger_text || "").match(/\$[0-9,]+\.[0-9]{2}/);
  if (m) return m[0];
  if (c.case.exposure_usd) return "$" + c.case.exposure_usd.toFixed(2);
  return "—";
}
function modelRisk(c) {
  const m = (c.trigger_text || "").match(/risk score (\d\.\d+)/i);
  return m ? parseFloat(m[1]) : null;
}
function beforeProb(c) {
  const m = (c.next_best_actions.what_changed || "").match(/from (\d\.\d+) to (\d\.\d+)/);
  return m ? parseFloat(m[1]) : null;
}
function uncertainty(c) {
  return 1 - Math.abs(c.case.fraud_probability - 0.5) * 2; // 1 = maximally uncertain
}
function isRing(c) {
  return c.case.pattern === "undocumented" && c.case.connected_card_ids.length > 0;
}
function riskColor(v) {
  if (v >= 0.7) return VERDICT.fraud;
  if (v >= 0.45) return VERDICT.uncertain;
  return VERDICT.legitimate;
}

// The cited tool calls, recovered from evidence refs (query:… / document:…).
function toolTrace(c) {
  const seen = new Set();
  const out = [];
  for (const e of c.case.evidence || []) {
    const ref = e.ref || "";
    if (ref.startsWith("query:")) {
      const mm = ref.slice(6).match(/^([a-z_]+)\((.*)\)$/);
      const name = mm ? mm[1] : ref.slice(6);
      const args = mm ? mm[2] : "";
      const key = "q:" + name + args;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ kind: "gsql", name, args });
    } else if (ref.startsWith("document:")) {
      const key = "d:" + ref;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ kind: "vector", name: "search_documents", args: ref.slice(9) });
    }
  }
  return out;
}

// A truthful investigation timeline assembled from the real fields.
function timeline(c) {
  const k = c.case;
  const ev = c.evidence_requests[0];
  const bp = beforeProb(c);
  const rows = [
    { actor: "System", t: c.opened_at, title: "Alert triggered",
      body: `${TRIGGER_LABEL[c.trigger_type] || c.trigger_type} · ${c.trigger_text}` },
    { actor: "Agent", title: "Investigated",
      body: `Pulled the transaction window and the cardholder baseline over ${c.tool_calls} tool calls via TigerGraph MCP.` },
  ];
  if (k.similar_prior_cases.length)
    rows.push({ actor: "Agent", title: "Retrieved memory",
      body: `Vector search returned ${k.similar_prior_cases.length} similar prior cases: ${k.similar_prior_cases.slice(0, 4).join(", ")}${k.similar_prior_cases.length > 4 ? "…" : ""}.` });
  if (isRing(c))
    rows.push({ actor: "Agent", title: "Ran ring_detect",
      body: `Connected-components over shared device profiles surfaced ${k.connected_card_ids.length} cards on one profile — new to every account.` });
  rows.push({ actor: "Agent", title: "Initial assessment",
    body: `Fraud probability ${(bp ?? k.fraud_probability).toFixed(2)}. Recommendation snapshotted before requesting evidence.` });
  if (ev)
    rows.push({ actor: "Agent", title: "Requested evidence",
      body: `${ev.type.replace(/_/g, " ")} (after step ${ev.asked_after_step}).` });
  if (ev)
    rows.push({ actor: c.trigger_type === "analyst_request" ? "Analyst" : "Customer", title: "Response received",
      body: ev.assumed_response });
  rows.push({ actor: "Agent", title: "Re-assessed",
    body: c.next_best_actions.what_changed || "No change after evidence." });
  rows.push({ actor: "Policy Engine", title: "Decided actions",
    body: `${c.next_best_actions.final.map((a) => a.action).join(", ")}${c.sar.file ? " · SAR filed" : ""}.` });
  rows.push({ actor: "System", title: "Case " + k.status.replace(/_/g, " "), body: c.stop_reason });
  return rows;
}

const ACTOR_COLOR = { System: "#868e96", Agent: "#4c6ef5", Analyst: "#7048e8", Customer: "#0ca678", "Policy Engine": "#f0731a" };

/* ---------- small UI atoms ---------- */
function Chip({ text, color, title, soft }) {
  return (
    <span className={"chip" + (soft ? " chip-soft" : "")} style={soft ? { color, borderColor: color } : { background: color }} title={title}>
      {text}
    </span>
  );
}
function Bar({ value, label, up }) {
  const pct = Math.round(value * 100);
  return (
    <div className="bar-wrap">
      <div className="bar-head">
        <span>{label}</span>
        {up != null && <span className="bar-delta">{up >= 0 ? "▲" : "▼"} {Math.abs(up)}</span>}
      </div>
      <div className="bar-track"><div className="bar-fill" style={{ width: pct + "%", background: riskColor(value) }} /></div>
      <div className="bar-num">{pct}/100</div>
    </div>
  );
}
function Action({ a, n }) {
  return (
    <div className="rec">
      <div className="rec-top">
        {n != null && <span className="rec-n">#{n}</span>}
        <span className="rec-name">{a.action}</span>
        <Chip text={a.route} color={ROUTE[a.route] || "#888"} />
      </div>
      <div className="rec-reason">{a.reason}</div>
    </div>
  );
}

/* ---------- app ---------- */
export default function App() {
  const cases = useMemo(() => [...casesData], []);
  const [selected, setSelected] = useState(cases.find((x) => x.case_id === "HHG-014")?.case_id || cases[0].case_id);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("risk");
  const [vf, setVf] = useState("all");
  const [tab, setTab] = useState("graph");
  const [phase, setPhase] = useState("after");
  const [actor, setActor] = useState("All");

  const stats = useMemo(() => {
    const s = { fraud: 0, legitimate: 0, uncertain: 0, sar: 0 };
    for (const x of cases) { s[x.case.verdict]++; if (x.sar.file) s.sar++; }
    return s;
  }, [cases]);

  const queue = useMemo(() => {
    let list = cases.filter((x) => vf === "all" || x.case.verdict === vf);
    if (q.trim()) {
      const t = q.toLowerCase();
      list = list.filter((x) =>
        x.case_id.toLowerCase().includes(t) ||
        x.case.pattern.toLowerCase().includes(t) ||
        findCardId(x).toLowerCase().includes(t));
    }
    const cmp = {
      risk: (a, b) => b.case.fraud_probability - a.case.fraud_probability,
      recency: (a, b) => (b.opened_at || "").localeCompare(a.opened_at || ""),
      uncertainty: (a, b) => uncertainty(b) - uncertainty(a),
    }[sort];
    return [...list].sort(cmp);
  }, [cases, q, sort, vf]);

  const c = cases.find((x) => x.case_id === selected);
  const k = c.case;
  const bp = beforeProb(c);
  const showPhase = phase === "before" && bp != null ? "before" : "after";
  const shownProb = showPhase === "before" ? bp : k.fraud_probability;
  const shownActions = showPhase === "before" ? c.next_best_actions.initial : c.next_best_actions.final;
  const mr = modelRisk(c);
  const tl = timeline(c).filter((r) => actor === "All" || r.actor === actor);
  const tt = toolTrace(c);

  return (
    <div className="shell">
      {/* top status bar */}
      <header className="topbar">
        <div className="tb-brand">
          <span className="glyph" aria-hidden="true" />
          <span className="tb-name">TigerGraph <b>Agentic Fraud Shield</b></span>
        </div>
        <div className="tb-stats">
          <div><span>TOTAL</span><b>20</b></div>
          <div><span>FRAUD</span><b style={{ color: VERDICT.fraud }}>{stats.fraud}</b></div>
          <div><span>LEGIT</span><b style={{ color: VERDICT.legitimate }}>{stats.legitimate}</b></div>
          <div><span>AMBIGUOUS</span><b style={{ color: VERDICT.uncertain }}>{stats.uncertain}</b></div>
          <div><span>SAR FILED</span><b>{stats.sar}</b></div>
        </div>
        <div className="tb-pills">
          {["TigerGraph MCP", "LLM Reasoning", "Policy Engine", "Vector Store"].map((p) => (
            <span className="pill" key={p}><i />{p}</span>
          ))}
        </div>
      </header>

      <div className="body">
        {/* left: case queue */}
        <aside className="queue">
          <div className="q-head">
            <span>CASE QUEUE</span><b>{queue.length}</b>
          </div>
          <input className="q-filter" placeholder="Filter by case, card or pattern…" value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="q-row">
            {["all", "fraud", "legitimate", "uncertain"].map((v) => (
              <button key={v} className={"q-chip" + (vf === v ? " on" : "")} onClick={() => setVf(v)}
                      style={vf === v && v !== "all" ? { color: VERDICT[v], borderColor: VERDICT[v] } : undefined}>
                {v === "all" ? "All" : v === "uncertain" ? "Ambiguous" : v[0].toUpperCase() + v.slice(1)}
              </button>
            ))}
          </div>
          <div className="q-row q-sort">
            <span>Sort</span>
            {["risk", "recency", "uncertainty"].map((s) => (
              <button key={s} className={"q-chip" + (sort === s ? " on" : "")} onClick={() => setSort(s)}>
                {s[0].toUpperCase() + s.slice(1)}
              </button>
            ))}
          </div>
          <div className="q-list">
            {queue.map((x) => {
              const p = x.case.fraud_probability;
              return (
                <button key={x.case_id} className={"q-item" + (x.case_id === selected ? " active" : "")} onClick={() => setSelected(x.case_id)}>
                  <div className="qi-top">
                    <span className="dot" style={{ background: VERDICT[x.case.verdict] }} />
                    <span className="qi-id">{x.case_id}</span>
                    <span className="qi-amt">{flaggedAmount(x)}</span>
                    <span className="qi-prob" style={{ color: riskColor(p) }}>{p.toFixed(2)}</span>
                  </div>
                  <div className="qi-mid">
                    <span className="qi-card">{findCardId(x)}</span>
                    <span className="qi-pat">{x.case.pattern.replace(/_/g, " ")}</span>
                  </div>
                  <div className="qi-tags">
                    <Chip text={VERDICT_LABEL[x.case.verdict]} color={VERDICT[x.case.verdict]} soft />
                    {x.sar.file && <span className="sarflag">SAR</span>}
                    {x.evidence_requests.length > 0 && x.case.verdict === "uncertain" && <span className="need">needs evidence</span>}
                  </div>
                  <div className="qi-bar"><div style={{ width: Math.round(p * 100) + "%", background: riskColor(p) }} /></div>
                </button>
              );
            })}
          </div>
        </aside>

        {/* center: case detail */}
        <main className="detail">
          <div className="d-head">
            <div className="d-title">
              <h1>{c.case_id}</h1>
              <Chip text={VERDICT_LABEL[k.verdict]} color={VERDICT[k.verdict]} />
              <span className="d-trigger">{TRIGGER_LABEL[c.trigger_type] || c.trigger_type}</span>
              <span className="d-status">{k.status.replace(/_/g, " ")}</span>
            </div>
            <div className="d-facts">
              <div><span>CARD</span><b>{findCardId(c)}</b></div>
              <div><span>AMOUNT</span><b>{flaggedAmount(c)}</b></div>
              <div><span>EXPOSURE</span><b>${k.exposure_usd.toFixed(2)}</b></div>
              {mr != null && <div><span>MODEL RISK</span><b style={{ color: riskColor(mr) }}>{mr.toFixed(2)}</b></div>}
              <div><span>PATTERN</span><b>{k.pattern.replace(/_/g, " ")}</b></div>
            </div>
            <p className="d-triggertext">{c.trigger_text}</p>
          </div>

          <div className="tabs">
            {[["graph", "Graph"], ["timeline", "Timeline"], ["evidence", "Evidence"], ["trace", "Tool Trace"]].map(([id, label]) => (
              <button key={id} className={"tab" + (tab === id ? " active" : "")} onClick={() => setTab(id)}>{label}</button>
            ))}
          </div>

          <section className="panel tab-body">
            {tab === "graph" && (
              <>
                <div className="algo-row">
                  {isRing(c) && <span className="algo"><b>WCC</b> {k.connected_card_ids.length} cards share 1 device profile</span>}
                  {isRing(c) && <span className="algo"><b>ring_detect</b> new to every account · anonymising proxy</span>}
                  {k.similar_prior_cases.length > 0 && <span className="algo"><b>kNN-Vector</b> {k.similar_prior_cases.length} memory neighbours retrieved</span>}
                  {!isRing(c) && k.similar_prior_cases.length === 0 && <span className="algo"><b>baseline</b> compared against cardholder history</span>}
                </div>
                <CaseGraph c={c} />
                <p className="cg-caption">The case as the TigerGraph knowledge graph holds it — the flagged card, its affected transactions, and (for a ring) the shared device profile fanning out to every connected card. Built from the same fields the agent wrote to the answer file.</p>
              </>
            )}

            {tab === "timeline" && (
              <>
                <div className="tl-actors">
                  {["All", "Agent", "Analyst", "Customer", "Policy Engine", "System"].map((a) => (
                    <button key={a} className={"q-chip" + (actor === a ? " on" : "")} onClick={() => setActor(a)}>{a}</button>
                  ))}
                </div>
                <div className="tl">
                  {tl.map((r, i) => (
                    <div className="tl-row" key={i}>
                      <span className="tl-dot" style={{ background: ACTOR_COLOR[r.actor] }} />
                      <div className="tl-content">
                        <div className="tl-line">
                          <span className="tl-actor" style={{ color: ACTOR_COLOR[r.actor] }}>{r.actor}</span>
                          <span className="tl-titletxt">{r.title}</span>
                          {r.t && <span className="tl-time">{r.t}</span>}
                        </div>
                        <div className="tl-body">{r.body}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}

            {tab === "evidence" && (
              <div className="ev-list">
                {k.evidence.map((e, i) => (
                  <div className="evidence" key={i}>
                    <Chip text={e.source} color={SOURCE[e.source] || "#888"} />
                    <div className="ev-body">
                      <div className="ev-claim">{e.claim}</div>
                      <div className="ev-ref">{e.ref}{e.entity_ids.length > 0 && " · " + e.entity_ids.slice(0, 8).join(", ")}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {tab === "trace" && (
              <div className="trace">
                {tt.map((r, i) => (
                  <div className="trace-row" key={i}>
                    <span className={"trace-kind " + r.kind}>{r.kind === "gsql" ? "GSQL" : "VECTOR"}</span>
                    <span className="trace-call"><b>{r.name}</b>{r.args && <span className="trace-args">({r.args})</span>}</span>
                    <span className="trace-via">via MCP</span>
                  </div>
                ))}
                <div className="trace-foot">
                  {c.tool_calls} tool calls · {c.tokens.toLocaleString()} tokens · {c.latency_s}s · stop: {c.stop_reason}
                </div>
              </div>
            )}
          </section>

          {/* SAR panel */}
          <section className="panel">
            <h2>Suspicious activity report</h2>
            {c.sar.file ? (
              <div className="sar">
                <div className="sar-meta">{c.sar.reason} · ${c.sar.total_amount_usd.toFixed(2)} · {c.sar.activity_dates.join(" to ")}</div>
                <p className="sar-narr">{c.sar.narrative}</p>
                <div className="muted">subjects: {c.sar.subjects.slice(0, 10).join(", ")}{c.sar.subjects.length > 10 ? " …" : ""}</div>
              </div>
            ) : (
              <div className="muted">No report filed. {c.sar.reason}</div>
            )}
          </section>
        </main>

        {/* right: AI reasoning */}
        <aside className="reason">
          <div className="r-head"><span className="r-glyph" />AI Reasoning</div>
          <div className="r-verdict">
            <span>VERDICT</span>
            <b style={{ color: VERDICT[k.verdict] }}>{VERDICT_LABEL[k.verdict]}</b>
          </div>
          <Bar label="Fraud probability" value={shownProb} />
          {mr != null && <Bar label="Model risk score" value={mr} />}

          {bp != null && (
            <div className="phase">
              <button className={"phase-btn" + (showPhase === "before" ? " on" : "")} onClick={() => setPhase("before")}>Before Evidence</button>
              <button className={"phase-btn" + (showPhase === "after" ? " on" : "")} onClick={() => setPhase("after")}>After Evidence</button>
            </div>
          )}
          {bp != null && showPhase === "before" && (
            <div className="phase-note">Snapshot before the agent requested evidence — probability {bp.toFixed(2)}.</div>
          )}
          {c.next_best_actions.what_changed && (c.next_best_actions.what_changed || "").toLowerCase() !== "nothing" && (
            <div className="phase-note changed"><b>What changed:</b> {c.next_best_actions.what_changed}</div>
          )}

          <div className="r-actions-head">RECOMMENDED ACTIONS <span>{shownActions.length}</span></div>
          {shownActions.map((a, i) => <Action key={i} a={a} n={i + 1} />)}

          <div className="r-foot">
            {k.written_to_graph ? `written to graph · ${k.graph_case_id}` : "not written to graph"}
          </div>
        </aside>
      </div>
    </div>
  );
}
