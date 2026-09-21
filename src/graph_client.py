"""TigerGraph connection, shared by the loader, the tools and the MCP server.

Savanna 4.2 does not speak the classic secret-as-password flow that
pyTigerGraph reaches for by default. Its GSQL service sits behind one HTTPS
port and authenticates with a JWT:

    POST /gsql/v1/tokens        {"secret": ...}   -> JWT
    POST /gsql/v1/statements    Bearer <jwt>      -> GSQL
    GET  /restpp/...            Bearer <jwt>      -> data API

Port 9000 is not exposed and /requesttoken does not exist, so pyTigerGraph
fails with "User authentication failed" no matter how the secret is passed.
This module talks to those endpoints directly and hands a pre-authenticated
pyTigerGraph connection to callers that want one.

Community Edition is still supported: set TG_SECRET empty and give
TG_USERNAME/TG_PASSWORD, and the classic path is used instead.

    python src/graph_client.py      # connection smoke test
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import requests  # noqa: F401  (also used for its exception types)

ROOT = Path(__file__).resolve().parent.parent

#: Values left at their template text must be treated as unset, or they get
#: sent as real credentials and the failure looks like a rejected password
#: rather than an unfilled field.
PLACEHOLDERS = {
    "your-password", "your-key", "your-secret", "paste-the-secret-here",
    "your-gemini-key", "changeme", "xxxx",
}

TOKEN_LIFETIME = 2_592_000  # 30 days, comfortably past the deadline

#: Phrases GSQL uses when it refuses a statement but still answers HTTP 200.
FAILURE_MARKERS = (
    "semantic check fails",
    "syntax error",
    "failed to create",
    "does not exist",
    "cannot be found",
    "reserved keyword",
    "please use another",
    "is not a valid",
    "no graph available",
    "was expecting",          # parser rejections print the expected tokens
    "lexical error",
    "used by another object",
)

#: Phrases GSQL uses to acknowledge DDL it actually applied.
SUCCESS_MARKERS = (
    "successfully created",
    "successfully dropped",
    "successfully added",
    "is created",
    "successfully updated",
    "added to graph",
)


def load_env(path: Path | None = None) -> dict[str, str]:
    """Read .env without requiring python-dotenv."""
    path = path or ROOT / ".env"
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            env[k.strip()] = "" if v.lower() in PLACEHOLDERS else v
    for k, v in env.items():
        if v:
            os.environ.setdefault(k, v)
    return env


class GsqlError(RuntimeError):
    """A GSQL statement came back with a semantic or syntax failure.

    GSQL returns HTTP 200 for statements it refused to run, so the body has to
    be inspected rather than the status code.
    """


class TigerGraph:
    """Thin authenticated client. One token, reused."""

    def __init__(
        self,
        host: str | None = None,
        secret: str | None = None,
        graph: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        env = load_env()
        self.host = (host or env.get("TG_HOST", "")).rstrip("/")
        self.secret = secret if secret is not None else env.get("TG_SECRET", "")
        self.graph = graph or env.get("TG_GRAPH") or "FraudGraph"
        self.username = username or env.get("TG_USERNAME") or "tigergraph"
        self.password = password if password is not None else env.get("TG_PASSWORD", "")
        if not self.host:
            raise RuntimeError("TG_HOST is not set; fill in .env")
        self._token: str | None = None
        self._session = requests.Session()

    # -- auth ------------------------------------------------------------
    @property
    def token(self) -> str:
        if self._token is None:
            self._token = self._mint_token()
        return self._token

    def _mint_token(self, retries: int = 5) -> str:
        """Mint a JWT, retrying while the workspace wakes.

        A suspended Savanna workspace answers the token endpoint with 502/503
        (and a connection reset) for tens of seconds as it comes back. The
        query path already retries these; the token path did not, so a run
        whose workspace suspended mid-way lost every remaining case on
        "token request failed [502]". Retry here too.
        """
        if not self.secret:
            return ""
        last = ""
        for attempt in range(retries):
            try:
                r = self._session.post(
                    f"{self.host}/gsql/v1/tokens",
                    json={"secret": self.secret, "lifetime": TOKEN_LIFETIME},
                    timeout=30,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = f"{type(exc).__name__}"
                if attempt == retries - 1:
                    break
                time.sleep(15 * (attempt + 1))
                continue
            if r.status_code == 200:
                payload = r.json()
                if payload.get("error"):
                    raise RuntimeError(f"token request refused: {payload.get('message')}")
                return payload["token"]
            last = f"[{r.status_code}] {r.text[:120]}"
            if r.status_code in (502, 503, 504) and attempt < retries - 1:
                wait = 15 * (attempt + 1)
                print(f"    graph: token {r.status_code}, workspace waking; "
                      f"retry in {wait}s", flush=True)
                time.sleep(wait)
                continue
            break
        raise RuntimeError(f"token request failed {last}")

    def _auth(self) -> dict[str, Any]:
        if self.secret:
            return {"headers": {"Authorization": f"Bearer {self.token}"}}
        return {"auth": (self.username, self.password)}

    # -- GSQL ------------------------------------------------------------
    def gsql(self, statement: str, graph: str | None = None, timeout: int = 600) -> str:
        """Run a GSQL statement. Raises GsqlError on a refusal."""
        body = statement
        target = graph if graph is not None else None
        if target:
            body = f"USE GRAPH {target}\n{statement}"
        kw = self._auth()
        headers = {"Content-Type": "text/plain", **kw.pop("headers", {})}
        r = self._session.post(
            f"{self.host}/gsql/v1/statements",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=timeout,
            **kw,
        )
        if r.status_code != 200:
            raise GsqlError(f"[{r.status_code}] {r.text[:400]}")
        text = r.text
        low = text.lower()
        for marker in FAILURE_MARKERS:
            if marker in low:
                raise GsqlError(text.strip()[:600])
        return text

    def ddl(self, statement: str, graph: str | None = None, timeout: int = 600) -> str:
        """Run a DDL statement and require an explicit success acknowledgement.

        GSQL answers a refusal with HTTP 200 and a plain sentence, so a
        blacklist of failure phrases will always be one unseen phrase away
        from reporting a silent no-op as a success. (It was: "The specified
        Identifier 'Case' is a reserved keyword" matched nothing, and the
        vertex and its four edges went missing while every step printed ok.)
        DDL is small and finite, so require the success marker instead.
        """
        text = self.gsql(statement, graph=graph, timeout=timeout)
        if not any(m in text.lower() for m in SUCCESS_MARKERS):
            raise GsqlError(text.strip()[:600] or "no acknowledgement from GSQL")
        return text

    def try_gsql(self, statement: str, graph: str | None = None) -> tuple[bool, str]:
        """Run a statement, reporting failure instead of raising."""
        try:
            return True, self.gsql(statement, graph=graph)
        except GsqlError as exc:
            return False, str(exc)

    # -- REST++ ----------------------------------------------------------
    def rest(self, method: str, path: str, retries: int = 4, **kw) -> Any:
        """REST++ call that survives a workspace waking up.

        Savanna free-tier workspaces auto-suspend, and they suspend during
        the long gaps while the agent is waiting on an LLM. The first call
        afterwards is refused outright -- a connection reset, not an HTTP
        error -- and the workspace then takes tens of seconds to come back.
        Without this, an investigation that has done all its work fails on
        the final write.
        """
        timeout = kw.pop("timeout", 120)
        last: Exception | None = None
        for attempt in range(retries):
            auth = self._auth()
            headers = {**auth.pop("headers", {}), **kw.get("headers", {})}
            call = {k: v for k, v in kw.items() if k != "headers"}
            try:
                r = self._session.request(
                    method, f"{self.host}/restpp{path}", headers=headers,
                    timeout=timeout, **auth, **call,
                )
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
                self._token = None          # a resumed workspace may reject the old token
                if attempt == retries - 1:
                    break
                wait = 15 * (attempt + 1)
                print(f"    graph: {type(exc).__name__}, workspace may be waking; "
                      f"retrying in {wait}s", flush=True)
                time.sleep(wait)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code in (502, 503, 504):
                    last = exc
                    if attempt == retries - 1:
                        break
                    time.sleep(10 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(f"graph unreachable after {retries} attempts: {last}")

    # -- helpers ---------------------------------------------------------
    def graphs(self) -> list[str]:
        out = self.gsql("LS")
        names: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("- Graph "):
                names.append(line[len("- Graph "):].split("(")[0].strip())
        return names

    def graph_exists(self, name: str | None = None) -> bool:
        return (name or self.graph) in self.graphs()

    def vertex_counts(self) -> dict[str, int]:
        try:
            data = self.rest("GET", f"/graph/{self.graph}/statistics?seconds=10")
        except Exception:  # noqa: BLE001
            data = None
        if not data:
            return {}
        return data

    def pytg(self):
        """A pyTigerGraph connection carrying our JWT, for its query helpers."""
        import pyTigerGraph as tg

        conn = tg.TigerGraphConnection(
            host=self.host,
            graphname=self.graph,
            username=self.username,
            password=self.password or self.secret,
            apiToken=self.token if self.secret else None,
        )
        if self.secret:
            conn.apiToken = self.token
        return conn


def connect(graph: str | None = None) -> TigerGraph:
    return TigerGraph(graph=graph)


# --------------------------------------------------------------------------
# Smoke test
# --------------------------------------------------------------------------

def main() -> int:
    env = load_env()
    tg = TigerGraph()
    print(f"host  : {tg.host}")
    print(f"graph : {tg.graph}")
    print(f"auth  : {'secret -> JWT' if tg.secret else 'username/password'}\n")

    try:
        r = requests.get(f"{tg.host}/restpp/echo", timeout=30)
        print(f"  OK   reachable: {r.json().get('message')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL unreachable: {str(exc)[:200]}")
        print(
            "\nCheck, in order:\n"
            "  1. workspace status is Ready, not Pending or Suspended\n"
            "  2. Network Access -> IP Allowlist includes your IP (or 0.0.0.0/0)\n"
            "  3. TG_HOST is the endpoint, with https:// and no trailing slash"
        )
        return 1

    try:
        t0 = time.time()
        tok = tg.token
        print(f"  OK   token: {tok[:18]}... ({time.time() - t0:.1f}s)")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL token: {str(exc)[:300]}")
        print("\nThe secret was rejected. Create a fresh one under Database Secrets.")
        return 1

    try:
        graphs = tg.graphs()
        print(f"  OK   graphs: {graphs or '(none yet)'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL LS: {str(exc)[:300]}")
        return 1

    if tg.graph in graphs:
        ok, out = tg.try_gsql("LS", graph=tg.graph)
        n = out.count("- VERTEX ") if ok else 0
        print(f"  OK   graph {tg.graph!r} exists with {n} vertex type(s)")
        print("\nConnected. Schema is in place.")
    else:
        print(f"\n  --   graph {tg.graph!r} does not exist yet")
        print("Connected. Next: python src/create_schema.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
