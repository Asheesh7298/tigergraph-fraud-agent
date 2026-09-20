"""Install and install-check the GSQL queries in graph/queries/.

    python src/create_queries.py              # install all, then install
    python src/create_queries.py --only ring_detect
    python src/create_queries.py --list       # show what is installed

Queries are dropped and recreated each run so the files on disk are always
the source of truth. INSTALL is the slow part (TigerGraph compiles them), so
all queries are installed in one call rather than one at a time.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_client import GsqlError, TigerGraph  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QDIR = ROOT / "graph" / "queries"


def split_queries(text: str) -> list[tuple[str, str]]:
    """Split a .gsql file into (name, source) per CREATE QUERY.

    A file may hold more than one query -- vector_search.gsql defines both
    search_documents and search_closed_cases. Sending the whole file as a
    single statement means one query's compile error is attributed to the
    file, and the other is created without ever being named, so it is never
    dropped on the next run.
    """
    text = text.lstrip("﻿")
    marks = [
        (m.start(), m.group(1))
        for m in re.finditer(r"CREATE\s+(?:OR\s+REPLACE\s+)?QUERY\s+(\w+)", text, re.I)
    ]
    if not marks:
        raise ValueError("no CREATE QUERY found")
    out: list[tuple[str, str]] = []
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((name, text[pos:end].strip()))
    return out


def installed(tg: TigerGraph) -> list[str]:
    """Query names LS reports for this graph.

    LS prints them under a "Queries:" heading as
        - name(param, param) (installed v2)
    so they have to be read out of that block rather than looked for as
    CREATE statements. Getting this wrong is silent: nothing is dropped, and
    every recreate then fails with "used by another object".
    """
    try:
        out = tg.gsql("LS", graph=tg.graph)
    except GsqlError:
        return []
    block = out.split("Queries:", 1)
    if len(block) < 2:
        return []
    return re.findall(r"^\s*-\s*(\w+)\s*\(", block[1], re.M)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="install just this query")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    tg = TigerGraph()
    print(f"host  : {tg.host}")
    print(f"graph : {tg.graph}\n")

    if args.list:
        for q in installed(tg):
            print(f"  {q}")
        return 0

    files = sorted(QDIR.glob("*.gsql"))
    defs: list[tuple[str, str]] = []
    for path in files:
        defs.extend(split_queries(path.read_text(encoding="utf-8-sig")))
    if args.only:
        defs = [(n, s) for n, s in defs if n == args.only]
        if not defs:
            print(f"no such query: {args.only}")
            return 1

    existing = set(installed(tg))
    created: list[str] = []
    failed: list[tuple[str, str]] = []

    for name, source in defs:
        if name in existing:
            tg.try_gsql(f"DROP QUERY {name}", graph=tg.graph)
        try:
            tg.ddl(source, graph=tg.graph)
            print(f"  created  {name}")
            created.append(name)
        except GsqlError as exc:
            msg = str(exc).strip()
            print(f"  FAILED   {name}")
            for line in msg.splitlines()[:6]:
                if line.strip():
                    print(f"             {line[:150]}")
            failed.append((name, msg))

    if not created:
        return 1 if failed else 0

    print(f"\ninstalling {len(created)} quer{'y' if len(created) == 1 else 'ies'} "
          f"(TigerGraph compiles these; ~30-90s) ...", flush=True)
    t0 = time.time()
    try:
        tg.gsql(f"INSTALL QUERY {', '.join(created)}", graph=tg.graph, timeout=1200)
        print(f"  installed in {time.time() - t0:,.0f}s")
    except GsqlError as exc:
        print(f"  INSTALL FAILED: {str(exc)[:500]}")
        return 1

    print(f"\nready: {', '.join(sorted(installed(tg)))}")
    if failed:
        print(f"\n{len(failed)} quer{'y' if len(failed) == 1 else 'ies'} did not compile: "
              f"{', '.join(n for n, _ in failed)}")
        return 1
    print("\nNext: python src/tools.py --smoke")
    return 0


if __name__ == "__main__":
    sys.exit(main())
