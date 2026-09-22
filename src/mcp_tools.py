"""Route the agent's graph access through the TigerGraph MCP server at runtime.

The hackathon requires the agent to reach the graph *through* MCP, not just to
ship an MCP server that sits unused. McpClient launches src/mcp_server.py as a
real stdio subprocess and calls its tools; McpTools presents the same surface
the agent already uses (FraudTools' method names and return shapes), so the
Investigator is unchanged apart from being handed this instead of FraudTools.

The chain at runtime is:
    agent -> McpClient (stdio) -> mcp_server.py -> GSQL / REST -> TigerGraph

MCP's client is async; the agent is synchronous. McpClient runs one asyncio
loop in a background thread and marshals each call onto it, so callers stay
sync. On Windows, subprocess transport needs the Proactor loop (the default
policy on 3.8+), which we set explicitly to be safe.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import ToolLog, agentcase_payload, as_dt, fmt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SERVER = str(ROOT / "src" / "mcp_server.py")


class McpClient:
    """Synchronous facade over an MCP stdio session kept on a background loop."""

    def __init__(self, server: str = SERVER, cwd: str | None = None, timeout: float = 180) -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._cm = None
        self._client = None
        self._timeout = timeout
        self._submit(self._connect(server, cwd or str(ROOT))).result(timeout=timeout)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _connect(self, server: str, cwd: str) -> None:
        from mcp import Client, StdioServerParameters

        params = StdioServerParameters(
            command=sys.executable, args=[server], cwd=cwd,
        )
        self._cm = Client(params)
        self._client = await self._cm.__aenter__()

    def call(self, name: str, **args) -> Any:
        clean = {k: v for k, v in args.items() if v is not None}
        res = self._submit(self._client.call_tool(name, clean)).result(timeout=self._timeout)
        if getattr(res, "is_error", False):
            raise RuntimeError(f"MCP tool {name} error: {getattr(res, 'content', '')}")
        return res.structured_content

    def close(self) -> None:
        try:
            self._submit(self._cm.__aexit__(None, None, None)).result(timeout=30)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


class McpTools:
    """FraudTools-compatible surface whose every graph call goes through MCP."""

    def __init__(self, client: McpClient | None = None, log: ToolLog | None = None) -> None:
        self.mcp = client or McpClient()
        self.log = log or ToolLog()

    def _c(self, name: str, **args) -> Any:
        self.log.add(name, args)
        return self.mcp.call(name, **args)

    # -- reads (same signatures the agent already calls) -----------------
    def get_transaction(self, txn_id: str) -> dict[str, Any]:
        return self._c("get_transaction", txn_id=txn_id)

    def get_device_profile(self, profile_key: str) -> dict[str, Any]:
        return self._c("get_device_profile", profile_key=profile_key)

    def card_window(self, card_id: str, centre, hours: float = 72.0) -> dict[str, Any]:
        return self._c("card_window", card_id=card_id, centre_ts=fmt(as_dt(centre)), hours=hours)

    def card_window_between(self, card_id: str, start, end, limit: int = 800) -> dict[str, Any]:
        return self._c("card_window_between", card_id=card_id,
                       start_ts=fmt(as_dt(start)), end_ts=fmt(as_dt(end)), limit=limit)

    def customer_baseline(self, card_id: str, before, amount=None, product=None,
                          tolerance: float = 2.0) -> dict[str, Any]:
        return self._c("customer_baseline", card_id=card_id, before_ts=fmt(as_dt(before)),
                       amount=amount if amount is not None else -1.0, product=product or "")

    def device_neighbors(self, profile_key: str, start, end) -> dict[str, Any]:
        return self._c("device_neighbors", profile_key=profile_key,
                       start_ts=fmt(as_dt(start)), end_ts=fmt(as_dt(end)))

    def region_history(self, card_id: str, region: str, before, parallel_days: int = 3) -> dict[str, Any]:
        return self._c("region_history", card_id=card_id, region=region, before_ts=fmt(as_dt(before)))

    def ring_detect(self, start, end, min_cards: int = 3, top: int = 60) -> dict[str, Any]:
        return self._c("ring_detect", start_ts=fmt(as_dt(start)), end_ts=fmt(as_dt(end)),
                       min_cards=min_cards, top=top)

    def similar_closed_cases(self, card_id: str, pattern: str = "", exposure=None,
                             limit: int = 12) -> dict[str, Any]:
        return self._c("similar_closed_cases", card_id=card_id, pattern=pattern or "",
                       exposure=exposure if exposure is not None else -1.0)

    # search takes TEXT now: the MCP server embeds it (Gemini) behind the tool,
    # so retrieval is fully behind MCP and the agent hands over no query vector.
    def search_documents(self, query: str, k: int = 6, kind: str = "") -> list[dict[str, Any]]:
        return (self._c("search_documents", query=query, k=k) or {}).get("hits") or []

    def search_closed_cases(self, query: str, before, k: int = 8, outcome: str = "") -> list[dict[str, Any]]:
        return (self._c("search_closed_cases", query=query, before_ts=fmt(as_dt(before)), k=k)
                or {}).get("hits") or []

    def prior_agent_cases(self, limit: int = 40) -> list[dict[str, Any]]:
        return (self._c("prior_agent_cases", limit=limit) or {}).get("cases") or []

    # -- write ----------------------------------------------------------
    def write_case(self, answer: Any, graph_case_id: str | None = None) -> str:
        cid, attrs, edges = agentcase_payload(answer, graph_case_id)
        self.log.add("write_case", {"graph_case_id": cid})
        out = self.mcp.call("persist_case", cid=cid, attrs=attrs, edges=edges)
        return (out or {}).get("graph_case_id", cid)

    def close(self) -> None:
        self.mcp.close()
