// A per-case investigation graph: the real entities of the selected case laid
// out left-to-right as the knowledge graph sees them —
//   Customer → Card → Transaction(s) → Device profile → Connected cards
// drawn from the answer file's own fields (no backend). For a ring case this
// shows the shared device fanning out to the connected cards; for a simple
// case it's just the card and its flagged transaction(s).

const COL = { customer: 70, card: 240, txn: 435, device: 615, connected: 780 };
const H = 440;
const MID = H / 2;

const C = {
  customer: "#4c6ef5",
  card: "#f0731a",
  txn: "#e5484d",
  txnLegit: "#30a46c",
  device: "#7048e8",
  connected: "#9aa1ad",
  edge: "#333a47",
  edgeHot: "rgba(240,115,26,0.45)",
};

// card_id isn't a top-level field; recover it from an evidence ref
// (query:...(card_id=C13487-K1)) or the trigger text.
function findCardId(c) {
  for (const e of c.case.evidence || []) {
    const m = (e.ref || "").match(/card_id=([A-Za-z0-9-]+)/);
    if (m) return m[1];
  }
  const t = (c.trigger_text || "").match(/C\d+-K\d+/);
  if (t) return t[0];
  return c.case.connected_card_ids[0] || "card";
}

function spread(n, span) {
  if (n <= 1) return [MID];
  const step = Math.min(span / (n - 1), 62);
  const start = MID - (step * (n - 1)) / 2;
  return Array.from({ length: n }, (_, i) => start + i * step);
}

function Node({ x, y, r, fill, label, sub, title }) {
  return (
    <g>
      {title && <title>{title}</title>}
      <circle cx={x} cy={y} r={r} fill={fill} opacity="0.16" />
      <circle cx={x} cy={y} r={r} fill="none" stroke={fill} strokeWidth="1.6" />
      {label && (
        <text x={x} y={y + 3.5} textAnchor="middle" fontSize="10" fontFamily="var(--mono)" fill="#edeef1">
          {label}
        </text>
      )}
      {sub && (
        <text x={x} y={y + r + 13} textAnchor="middle" fontSize="9.5" fontFamily="var(--mono)" fill="#9aa1ad">
          {sub}
        </text>
      )}
    </g>
  );
}

function Edge({ x1, y1, x2, y2, hot, dashed }) {
  const mx = (x1 + x2) / 2;
  return (
    <path
      d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
      fill="none"
      stroke={hot ? C.edgeHot : C.edge}
      strokeWidth={hot ? 1.6 : 1.1}
      strokeDasharray={dashed ? "4 4" : "none"}
    />
  );
}

export default function CaseGraph({ c }) {
  const k = c.case;
  const isFraud = k.verdict === "fraud";
  const txC = isFraud ? C.txn : C.txnLegit;
  const cardId = findCardId(c);
  const customer = cardId.split("-K")[0];

  const allTxns = k.affected_txn_ids.length
    ? k.affected_txn_ids
    : k.first_suspicious_txn_id
    ? [k.first_suspicious_txn_id]
    : [];
  const txns = allTxns.slice(0, 6);
  const moreTxns = allTxns.length - txns.length;

  const hasDevice = k.connected_device_profiles.length > 0;
  const connected = k.connected_card_ids.slice(0, 8);
  const moreConnected = k.connected_card_ids.length - connected.length;

  const txnY = spread(txns.length + (moreTxns > 0 ? 1 : 0), 260);
  const connY = spread(connected.length + (moreConnected > 0 ? 1 : 0), 330);

  const empty = txns.length === 0 && !hasDevice;

  return (
    <div className="casegraph">
      <svg viewBox={`0 0 850 ${H}`} width="100%" preserveAspectRatio="xMidYMid meet" role="img"
           aria-label="Case entity graph">
        {/* edges (drawn under the nodes) */}
        <Edge x1={COL.customer + 20} y1={MID} x2={COL.card - 26} y2={MID} hot />
        {txns.map((t, i) => (
          <Edge key={"m" + i} x1={COL.card + 26} y1={MID} x2={COL.txn - 17} y2={txnY[i]} hot={isFraud} />
        ))}
        {hasDevice &&
          txns.map((t, i) => (
            <Edge key={"d" + i} x1={COL.txn + 17} y1={txnY[i]} x2={COL.device - 22} y2={MID} hot />
          ))}
        {hasDevice &&
          connected.map((cc, i) => (
            <Edge key={"c" + i} x1={COL.device + 22} y1={MID} x2={COL.connected - 9} y2={connY[i]} hot dashed />
          ))}
        {hasDevice && moreConnected > 0 && (
          <Edge x1={COL.device + 22} y1={MID} x2={COL.connected - 9} y2={connY[connected.length]} hot dashed />
        )}

        {/* nodes */}
        <text x={COL.customer} y={MID - 34} textAnchor="middle" fontSize="9" fontFamily="var(--mono)" fill="#9aa1ad">
          customer
        </text>
        <Node x={COL.customer} y={MID} r={20} fill={C.customer} label="CUST" sub={customer} title={"Customer " + customer} />

        <text x={COL.card} y={MID - 40} textAnchor="middle" fontSize="9" fontFamily="var(--mono)" fill="#9aa1ad">
          card
        </text>
        <Node x={COL.card} y={MID} r={26} fill={C.card} label="CARD" sub={cardId} title={"Card " + cardId} />

        {txns.length > 0 && (
          <text x={COL.txn} y={20} textAnchor="middle" fontSize="9" fontFamily="var(--mono)" fill="#9aa1ad">
            {isFraud ? "affected txns" : "flagged txn"}
          </text>
        )}
        {txns.map((t, i) => (
          <Node key={t} x={COL.txn} y={txnY[i]} r={16} fill={txC} label="txn" sub={t}
                title={"Transaction " + t + (k.first_suspicious_txn_id === t ? " (first suspicious)" : "")} />
        ))}
        {moreTxns > 0 && (
          <text x={COL.txn} y={txnY[txns.length] + 3} textAnchor="middle" fontSize="10"
                fontFamily="var(--mono)" fill="#f0731a">
            +{moreTxns}
          </text>
        )}

        {hasDevice && (
          <>
            <text x={COL.device} y={MID - 40} textAnchor="middle" fontSize="9" fontFamily="var(--mono)" fill="#9aa1ad">
              device profile
            </text>
            <Node x={COL.device} y={MID} r={22} fill={C.device} label="DEV" sub="shared"
                  title={k.connected_device_profiles[0]} />
          </>
        )}

        {hasDevice && (
          <text x={COL.connected} y={30} textAnchor="middle" fontSize="9" fontFamily="var(--mono)" fill="#9aa1ad">
            {k.connected_card_ids.length} cards on device
          </text>
        )}
        {hasDevice &&
          connected.map((cc, i) => (
            <Node key={cc} x={COL.connected} y={connY[i]} r={10} fill={C.connected} sub={cc} title={"Connected card " + cc} />
          ))}
        {hasDevice && moreConnected > 0 && (
          <text x={COL.connected} y={connY[connected.length] + 3} textAnchor="middle" fontSize="10"
                fontFamily="var(--mono)" fill="#f0731a">
            +{moreConnected}
          </text>
        )}

        {empty && (
          <text x="425" y={MID} textAnchor="middle" fontSize="11" fontFamily="var(--mono)" fill="#9aa1ad">
            legitimate — no affected transactions or connected entities
          </text>
        )}
      </svg>

      <div className="cg-legend">
        <span><i style={{ background: C.customer }} />Customer</span>
        <span><i style={{ background: C.card }} />Card</span>
        {txns.length > 0 && <span><i style={{ background: txC }} />Transaction</span>}
        {hasDevice && <span><i style={{ background: C.device }} />Device profile</span>}
        {hasDevice && <span><i style={{ background: C.connected }} />Connected card</span>}
        <span className="cg-note">edges — OWNS · MADE · FROM_DEVICE · shared&nbsp;device</span>
      </div>
    </div>
  );
}
