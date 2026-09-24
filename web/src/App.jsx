import { useMemo, useState } from "react";
import casesData from "./data/cases.json";
import CaseGraph from "./CaseGraph";

const VERDICT = {
  fraud: "#e5484d",
  legitimate: "#30a46c",
  uncertain: "#f5a623",
};
const ROUTE = { auto: "#8b8b8b", L1: "#d9820b", L2: "#e5484d" };
const SOURCE = { graph: "#4c6ef5", document: "#7048e8", customer: "#0ca678", external: "#868e96" };

function Chip({ text, color, title }) {
  return (
    <span className="chip" style={{ background: color }} title={title}>
      {text}
    </span>
  );
}

function Metric({ label, value, accent }) {
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className="metric-value" style={accent ? { color: accent } : undefined}>
        {value}
      </div>
    </div>
  );
}

function Action({ a }) {
  return (
    <div className="action">
      <Chip text={a.route} color={ROUTE[a.route] || "#888"} />
      <span className="action-name">{a.action}</span>
      <div className="action-reason">{a.reason}</div>
    </div>
  );
}

export default function App() {
  const cases = useMemo(
    () => [...casesData].sort((a, b) => a.case_id.localeCompare(b.case_id)),
    []
  );
  const [selected, setSelected] = useState(cases[0].case_id);
  const c = cases.find((x) => x.case_id === selected);
  const k = c.case;

  const stats = useMemo(() => {
    const s = { fraud: 0, legitimate: 0, uncertain: 0, sar: 0 };
    for (const x of cases) {
      s[x.case.verdict] = (s[x.case.verdict] || 0) + 1;
      if (x.sar.file) s.sar += 1;
    }
    return s;
  }, [cases]);

  const changed =
    (c.next_best_actions.what_changed || "").trim().toLowerCase() !== "nothing";

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="glyph" aria-hidden="true" />
          <div>
            <div className="brand-title">Fraud Investigation</div>
            <div className="brand-sub">TigerGraph · case review</div>
          </div>
        </div>
        <div className="portfolio">
          <div className="pstat"><b style={{ color: VERDICT.fraud }}>{stats.fraud}</b><span>fraud</span></div>
          <div className="pstat"><b style={{ color: VERDICT.legitimate }}>{stats.legitimate}</b><span>legit</span></div>
          <div className="pstat"><b style={{ color: VERDICT.uncertain }}>{stats.uncertain}</b><span>uncertain</span></div>
          <div className="pstat"><b>{stats.sar}</b><span>reports</span></div>
        </div>
        <div className="caselist">
          {cases.map((x) => (
            <button
              key={x.case_id}
              className={"caseitem" + (x.case_id === selected ? " active" : "")}
              onClick={() => setSelected(x.case_id)}
            >
              <span className="dot" style={{ background: VERDICT[x.case.verdict] }} />
              <span className="cid">{x.case_id}</span>
              <span className="cpat">{x.case.pattern}</span>
              {x.sar.file && <span className="sarflag">SAR</span>}
            </button>
          ))}
        </div>
      </aside>

      <main className="main">
        <header className="case-head">
          <div className="case-title">
            <h1>{c.case_id}</h1>
            <Chip text={k.verdict.toUpperCase()} color={VERDICT[k.verdict]} />
            <span className="trigger">{c.trigger_type}</span>
          </div>
          <p className="trigger-text">{c.trigger_text}</p>
        </header>

        <section className="metrics">
          <Metric label="Pattern" value={k.pattern} />
          <Metric label="Fraud probability" value={k.fraud_probability.toFixed(2)} />
          <Metric label="Exposure" value={"$" + k.exposure_usd.toLocaleString(undefined, { minimumFractionDigits: 2 })} />
          <Metric label="Status" value={k.status.replace("_", " ")} />
          <Metric label="Report" value={c.sar.file ? "filed" : "no"} accent={c.sar.file ? VERDICT.fraud : undefined} />
        </section>

        <section className="summary">{k.summary}</section>
        {k.pattern === "undocumented" && k.pattern_description && (
          <section className="pattern-desc">
            <span className="tag">undocumented pattern</span>
            {k.pattern_description}
          </section>
        )}

        <section className="panel">
          <h2>Investigation graph</h2>
          <CaseGraph c={c} />
          <p className="cg-caption">
            The case as the knowledge graph holds it — the flagged card, its
            affected transactions, and (for a ring) the shared device profile
            fanning out to every connected card. Built from the same fields the
            agent wrote to the answer file.
          </p>
        </section>

        <section className="panel">
          <h2>Next best action</h2>
          <div className="nba">
            <div className="nba-col">
              <div className="nba-label">Initial <span>before requesting evidence</span></div>
              {c.next_best_actions.initial.map((a, i) => <Action key={i} a={a} />)}
            </div>
            <div className="nba-col">
              <div className="nba-label">Final <span>after the response</span></div>
              {c.next_best_actions.final.map((a, i) => <Action key={i} a={a} />)}
            </div>
          </div>
          {changed && (
            <div className="what-changed">
              <b>What changed:</b> {c.next_best_actions.what_changed}
            </div>
          )}
          {c.evidence_requests.length > 0 && (
            <div className="evreq">
              {c.evidence_requests.map((r, i) => (
                <div key={i}>
                  <Chip text={r.type} color="#495057" /> after step {r.asked_after_step}:{" "}
                  <em>{r.assumed_response}</em>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="panel">
          <h2>Evidence</h2>
          {k.evidence.map((e, i) => (
            <div className="evidence" key={i}>
              <Chip text={e.source} color={SOURCE[e.source] || "#888"} />
              <div className="ev-body">
                <div className="ev-claim">{e.claim}</div>
                <div className="ev-ref">
                  {e.ref}
                  {e.entity_ids.length > 0 && " · " + e.entity_ids.slice(0, 8).join(", ")}
                </div>
              </div>
            </div>
          ))}
        </section>

        {(k.affected_txn_ids.length > 0 ||
          k.connected_card_ids.length > 0 ||
          k.connected_device_profiles.length > 0 ||
          k.similar_prior_cases.length > 0) && (
          <section className="panel grid2">
            {k.affected_txn_ids.length > 0 && (
              <div>
                <h3>Affected transactions ({k.affected_txn_ids.length})</h3>
                <div className="muted">first suspicious: {k.first_suspicious_txn_id}</div>
                <div className="idlist">{k.affected_txn_ids.join("  ")}</div>
              </div>
            )}
            {k.connected_card_ids.length > 0 && (
              <div>
                <h3>Connected cards ({k.connected_card_ids.length})</h3>
                <div className="idlist">{k.connected_card_ids.slice(0, 40).join("  ")}</div>
              </div>
            )}
            {k.connected_device_profiles.length > 0 && (
              <div className="span2">
                <h3>Device profile</h3>
                <div className="idlist">{k.connected_device_profiles.join("  ")}</div>
              </div>
            )}
            {k.similar_prior_cases.length > 0 && (
              <div className="span2">
                <h3>Prior cases used as memory</h3>
                <div className="idlist">{k.similar_prior_cases.join("  ")}</div>
              </div>
            )}
          </section>
        )}

        <section className="panel">
          <h2>Suspicious activity report</h2>
          {c.sar.file ? (
            <div className="sar">
              <div className="sar-meta">
                {c.sar.reason} · ${c.sar.total_amount_usd.toLocaleString(undefined, { minimumFractionDigits: 2 })} ·{" "}
                {c.sar.activity_dates.join(" to ")}
              </div>
              <p className="sar-narr">{c.sar.narrative}</p>
              <div className="muted">subjects: {c.sar.subjects.join(", ")}</div>
            </div>
          ) : (
            <div className="muted">No report filed. {c.sar.reason}</div>
          )}
        </section>

        <footer className="case-foot">
          <span>stop: {c.stop_reason}</span>
          <span className="foot-metrics">
            {c.tool_calls} tool calls · {c.tokens.toLocaleString()} tokens · {c.latency_s}s ·{" "}
            {k.written_to_graph ? `graph ✓ ${k.graph_case_id}` : "graph —"}
          </span>
        </footer>
      </main>
    </div>
  );
}
