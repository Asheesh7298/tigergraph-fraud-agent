"""Create (or recreate) the FraudGraph schema on the configured instance.

    python src/create_schema.py            # create if absent
    python src/create_schema.py --drop     # tear down and rebuild

GSQL returns HTTP 200 for statements it refuses, so every step is checked by
reading the response body, not the status code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_client import GsqlError, TigerGraph  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def statements(text: str) -> list[str]:
    """Split the .gsql file into statements, dropping comments.

    GSQL has no statement terminator for DDL of this kind, so each
    CREATE begins a new statement and blank-line-separated blocks are joined.
    """
    out: list[str] = []
    current: list[str] = []
    text = text.lstrip("﻿")   # editors and PowerShell both like to add a BOM
    for raw in text.splitlines():
        line = raw.split("//")[0].rstrip()
        if not line.strip():
            continue
        if line.upper().lstrip().startswith(("CREATE ", "DROP ", "ALTER ", "USE ")) and current:
            out.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        out.append("\n".join(current))
    return [s for s in out if s.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--drop", action="store_true", help="drop the graph first")
    ap.add_argument("--schema", type=Path, default=ROOT / "graph" / "schema.gsql")
    args = ap.parse_args()

    tg = TigerGraph()
    print(f"host  : {tg.host}")
    print(f"graph : {tg.graph}\n")

    existing = tg.graphs()
    if args.drop:
        # DROP GRAPH leaves the global vertex and edge types behind, so a
        # half-created type from an earlier failed run would survive and be
        # silently re-used by CREATE GRAPH ... (*). DROP ALL clears the
        # catalogue so the rebuild is genuinely from clean.
        print("dropping existing schema ...", flush=True)
        ok, out = tg.try_gsql("DROP ALL")
        print(f"  {'ok' if ok else 'note'}: {out.strip().splitlines()[-1][:160] if out.strip() else ''}")
        existing = tg.graphs()

    if tg.graph in existing and not args.drop:
        print(f"graph {tg.graph} already exists. Use --drop to rebuild.")
        ok, out = tg.try_gsql("LS", graph=tg.graph)
        print(out[:800] if ok else out)
        return 0

    stmts = statements(args.schema.read_text(encoding="utf-8-sig"))
    print(f"applying {len(stmts)} statement(s) from {args.schema.name} ...\n", flush=True)

    for i, stmt in enumerate(stmts, 1):
        head = stmt.splitlines()[0][:66]
        try:
            tg.ddl(stmt)   # requires an explicit success acknowledgement
            print(f"  [{i:2}/{len(stmts)}] ok   {head}")
        except GsqlError as exc:
            msg = str(exc).strip().replace("\n", " ")[:300]
            if "already exist" in msg.lower():
                print(f"  [{i:2}/{len(stmts)}] skip {head}  (exists)")
                continue
            print(f"  [{i:2}/{len(stmts)}] FAIL {head}\n        {msg}")
            return 1

    print()
    if tg.graph_exists():
        ok, out = tg.try_gsql("LS", graph=tg.graph)
        v = out.count("- VERTEX ")
        e = out.count("- DIRECTED EDGE ") + out.count("- UNDIRECTED EDGE ")
        print(f"graph {tg.graph} created: {v} vertex types, {e} edge types")
        print("\nNext: python src/load_graph.py")
        return 0

    print(f"graph {tg.graph} was not created. Output of LS:")
    print(tg.gsql("LS")[:600])
    return 1


if __name__ == "__main__":
    sys.exit(main())
