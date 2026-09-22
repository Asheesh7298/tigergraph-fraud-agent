"""TigerGraph MCP server -- exposes the fraud-investigation graph tools over MCP.

The hackathon requires TigerGraph MCP: the agent's graph capabilities are
served as Model Context Protocol tools rather than called only in-process. The
same FraudTools functions that back the agent back these tools, so the graph
surface an MCP client sees is exactly the one the investigation uses -- the
six GSQL queries, transaction lookup, and GraphRAG document search.

Run it as a stdio MCP server (what an MCP client launches):

    python src/mcp_server.py

Or exercise it without a client:

    python src/mcp_server.py --selftest

Vector search takes plain text here and embeds it with Gemini internally, so a
client never has to hand over a 768-float query vector.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from llm import Gemini  # noqa: E402
from tools import FraudTools  # noqa: E402

server = MCPServer("tigergraph-fraud")

_tools: FraudTools | None = None
_llm: Gemini | None = None


def tools() -> FraudTools:
    global _tools
    if _tools is None:
        _tools = FraudTools()
    return _tools


def llm() -> Gemini:
    global _llm
    if _llm is None:
        _llm = Gemini()
    return _llm


@server.tool(description="Look up one transaction by id, with its real timestamp, "
                         "amount, channel, card and device profile.")
def get_transaction(txn_id: str) -> dict[str, Any]:
    return tools().get_transaction(txn_id)


@server.tool(description="Ordered transactions on a card within +/- `hours` of a "
                         "centre timestamp (resolve the flagged txn's real ts first; "
                         "an alert can open hours after the activity).")
def card_window(card_id: str, centre_ts: str, hours: float = 72.0) -> dict[str, Any]:
    return tools().card_window(card_id, centre_ts, hours=hours)


@server.tool(description="A card's baseline before a moment: p50/p90/max amounts, "
                         "usual products/regions/devices, and how many prior "
                         "transactions match a given amount+product (the R7 "
                         "recurring-charge signal).")
def customer_baseline(card_id: str, before_ts: str, amount: float = -1.0,
                      product: str = "") -> dict[str, Any]:
    return tools().customer_baseline(
        card_id, before_ts,
        amount=amount if amount >= 0 else None,
        product=product or None,
    )


@server.tool(description="Every card and customer seen on a device profile within a "
                         "window, with new-to-account and anonymised-proxy counts.")
def device_neighbors(profile_key: str, start_ts: str, end_ts: str) -> dict[str, Any]:
    return tools().device_neighbors(profile_key, start_ts, end_ts)


@server.tool(description="Has this card billed in a region before, and did normal "
                         "home activity continue in parallel (trip vs cloned card).")
def region_history(card_id: str, region: str, before_ts: str) -> dict[str, Any]:
    return tools().region_history(card_id, region, before_ts)


@server.tool(description="Detect device-profile rings in a window WITHOUT a device "
                         "string: ranks profiles by cards x new-to-account x "
                         "anonymised-proxy. Returns the ranked clusters.")
def ring_detect(start_ts: str, end_ts: str, min_cards: int = 3, top: int = 60) -> dict[str, Any]:
    out = tools().ring_detect(start_ts, end_ts, min_cards=min_cards, top=top)
    return {"ranked": out.get("ranked", [])}


@server.tool(description="Prior closed cases connected to a card by shared entity "
                         "(same card, device, region) plus same-pattern precedents.")
def similar_closed_cases(card_id: str, pattern: str = "",
                         exposure: float = -1.0) -> dict[str, Any]:
    return tools().similar_closed_cases(
        card_id, pattern=pattern, exposure=exposure if exposure >= 0 else None
    )


@server.tool(description="GraphRAG: semantic search over the fraud policy, the "
                         "pattern typologies and the ring write-ups stored in "
                         "TigerGraph. Pass plain text; it is embedded internally.")
def search_documents(query: str, k: int = 5) -> dict[str, Any]:
    vec = llm().embed(query)
    return {"hits": tools().search_documents(vec, k=k)}


@server.tool(description="Attributes of one device profile (device, OS, browser, "
                         "screen, dominant proxy).")
def get_device_profile(profile_key: str) -> dict[str, Any]:
    return tools().get_device_profile(profile_key)


@server.tool(description="Ordered transactions on a card between two explicit "
                         "timestamps (wider than card_window, for ring episodes "
                         "spread over weeks).")
def card_window_between(card_id: str, start_ts: str, end_ts: str,
                        limit: int = 800) -> dict[str, Any]:
    return tools().card_window_between(card_id, start_ts, end_ts, limit=limit)


@server.tool(description="GraphRAG over closed-case narratives: prior cases whose "
                         "story resembles this one, closed before p_before. Pass "
                         "plain text; embedded internally.")
def search_closed_cases(query: str, before_ts: str, k: int = 5) -> dict[str, Any]:
    vec = llm().embed(query)
    return {"hits": tools().search_closed_cases(vec, before_ts, k=k)}


@server.tool(description="Cases this agent has already closed this run, as "
                         "cross-case memory (written back to the graph).")
def prior_agent_cases(limit: int = 40) -> dict[str, Any]:
    return {"cases": tools().prior_agent_cases(limit=limit)}


@server.tool(description="Persist an investigated case back to the graph as an "
                         "AgentCase vertex with its edges. Returns the vertex id.")
def persist_case(cid: str, attrs: dict[str, Any], edges: dict[str, Any]) -> dict[str, Any]:
    return {"graph_case_id": tools().persist_case(cid, attrs, edges or {})}


def selftest() -> int:
    """Exercise the server surface the way an MCP client would."""
    import anyio

    async def run() -> int:
        listed = await server.list_tools()
        names = [t.name for t in listed]
        print(f"tools exposed ({len(names)}): {', '.join(names)}")

        checks = [
            ("get_transaction", {"txn_id": "3478561"},
             lambda r: r.get("card_id") == "C13487-K1"),
            ("ring_detect",
             {"start_ts": "2016-11-01 00:00:00", "end_ts": "2016-12-31 23:59:59",
              "min_cards": 5},
             lambda r: r["ranked"] and r["ranked"][0]["n_cards"] == 28),
            ("search_documents", {"query": "customer disputes a recurring charge", "k": 3},
             lambda r: bool(r.get("hits"))),
        ]
        ok = True
        for name, args, check in checks:
            try:
                result = await server.call_tool(name, args)
                payload = result.structured_content
                good = check(payload)
                print(f"  {'OK  ' if good else 'FAIL'} {name}")
                ok = ok and good
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL {name}: {str(exc)[:100]}")
                ok = False
        return 0 if ok else 1

    return anyio.run(run)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    # Default: run as a stdio MCP server for a client to connect to.
    server.run(transport="stdio")
