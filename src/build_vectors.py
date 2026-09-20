"""Embed the retrieval corpus and the closed-case history into TigerGraph.

Two collections, answering different questions:

  Document    written guidance -- the fraud policy split by rule, the five
              pattern descriptions, the two ring typologies, the false-alarm
              calibration. Retrieved when the agent needs to know what the
              policy says about a situation.

  ClosedCase  the 5,565 finished investigations, embedded on their analyst
              narratives. Retrieved when the agent needs precedent.

Closed-case retrieval by vector complements `similar_closed_cases`, which
retrieves by connected entity. A case reached through a shared device profile
is evidence; a case reached through similar wording is a precedent. Both are
wanted, and they disagree often enough to be worth having separately.

    python src/build_vectors.py --docs        # corpus only (fast)
    python src/build_vectors.py --cases       # closed cases (~10 min)
    python src/build_vectors.py               # both
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_client import TigerGraph  # noqa: E402
from llm import EMBED_DIM, Gemini  # noqa: E402

csv.field_size_limit(10_000_000)

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
RAW = ROOT / "data" / "raw"

BATCH = 200          # vertices per upsert
MAX_SECTION = 4_000  # characters per Document chunk


def front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    _, fm, body = text.split("---", 2)
    meta: dict[str, str] = {}
    for line in fm.strip().splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body


def split_sections(body: str) -> Iterator[tuple[str, str]]:
    """Split markdown on headings, keeping each chunk retrievable on its own."""
    parts = re.split(r"^(#{1,3})\s+(.+)$", body, flags=re.M)
    if len(parts) <= 1:
        yield "", body.strip()
        return
    preamble = parts[0].strip()
    if preamble:
        yield "", preamble
    for i in range(1, len(parts), 3):
        heading = parts[i + 1].strip()
        content = parts[i + 2].strip() if i + 2 < len(parts) else ""
        if not content:
            continue
        for j in range(0, len(content), MAX_SECTION):
            suffix = "" if len(content) <= MAX_SECTION else f" (part {j // MAX_SECTION + 1})"
            yield heading + suffix, content[j:j + MAX_SECTION]


def policy_rules() -> Iterator[tuple[str, str, str]]:
    """The fraud policy, split so a single rule can be retrieved on its own.

    Retrieving "the policy" as one blob is useless -- it is thousands of
    words and the model has to find the relevant rule inside it. Retrieving
    R7 by itself, when the situation looks like R7, is the point.
    """
    # The fraud policy lives in the organizers' dataset spec. It sat at the
    # repo root as README.md during development; it now lives in docs/ so the
    # root README can describe the project. Fall back to the old location.
    spec_path = ROOT / "docs" / "DATASET.md"
    if not spec_path.exists():
        spec_path = ROOT / "README.md"
    readme = spec_path.read_text(encoding="utf-8")
    start = readme.find("# Fraud Policy")
    if start < 0:
        return
    policy = readme[start:readme.find("# Answer Format", start)]
    for m in re.finditer(
        r"\*\*(R\d+)\.\s*([^*]+?)\*\*(.*?)(?=\n\*\*R\d+\.|\Z)", policy, re.S
    ):
        rule, title, body = m.group(1), m.group(2).strip(), m.group(3).strip()
        yield f"policy-{rule.lower()}", f"{rule}. {title}", f"{rule}. {title}\n\n{body}"
    for name, pat in (
        ("actions", r"### 1\. Actions(.*?)### 2\."),
        ("routing", r"### 2\. Approval routing(.*?)### 3\."),
        ("case-vs-report", r"### 3a\.(.*?)### 3b\."),
        ("changing-action", r"### 3b\.(.*?)### 4\."),
        ("exposure", r"### 4\. Exposure(.*?)### 5\."),
        ("evidence", r"### 5\. Gathering more evidence(.*?)### 6\."),
        ("stopping", r"### 6\. Stopping(.*?)### 7\."),
        ("explaining", r"### 7\. Explaining(.*?)(?:---|\Z)"),
    ):
        m = re.search(pat, policy, re.S)
        if m:
            yield f"policy-{name}", f"Fraud policy: {name.replace('-', ' ')}", m.group(1).strip()


def collect_documents() -> list[dict[str, str]]:
    docs: list[dict[str, str]] = []

    for doc_id, title, content in policy_rules():
        docs.append({
            "doc_id": doc_id, "title": title, "kind": "policy",
            "source": "README.md", "section": "", "content": content,
        })

    for path in sorted(DOCS.glob("*.md")):
        meta, body = front_matter(path.read_text(encoding="utf-8"))
        base = meta.get("doc_id") or path.stem
        for i, (section, content) in enumerate(split_sections(body)):
            docs.append({
                "doc_id": f"{base}#{i}",
                "title": meta.get("title", path.stem),
                "kind": meta.get("kind", "typology"),
                "source": meta.get("source", path.name),
                "section": section,
                "content": content,
            })
    return docs


def case_text(r: dict[str, str]) -> str:
    """What to embed for a closed case.

    The analyst note carries the narrative, but the structured fields carry
    the shape -- pattern, outcome, size, whether it was reported. Embedding
    both means a query about "four large online charges in half an hour"
    can reach a case whose note does not use those words.
    """
    return (
        f"Outcome: {r['outcome']}. Pattern: {r['pattern']}. "
        f"{r['n_txns']} transaction(s), exposure ${r['exposure_usd']}. "
        f"Report filed: {r['report_filed']}. "
        f"Actions: {r['actions_taken'].replace('|', ', ')}. "
        f"{r['analyst_notes']}"
    )


def upsert(tg: TigerGraph, vtype: str, rows: dict[str, dict]) -> None:
    payload = {
        "vertices": {
            vtype: {
                vid: {k: {"value": v} for k, v in attrs.items()}
                for vid, attrs in rows.items()
            }
        }
    }
    for attempt in range(4):
        try:
            tg.rest("POST", f"/graph/{tg.graph}", json=payload, timeout=300)
            return
        except Exception:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def build_docs(tg: TigerGraph, g: Gemini) -> int:
    docs = collect_documents()
    print(f"corpus: {len(docs)} chunks from {len(list(DOCS.glob('*.md')))} files + the policy")
    texts = [f"{d['title']}\n{d['section']}\n{d['content']}" for d in docs]
    embs = g.embed_many(texts, progress_every=50)

    buf: dict[str, dict] = {}
    for d, e in zip(docs, embs):
        buf[d["doc_id"]] = {**d, "emb": e}
        buf[d["doc_id"]].pop("doc_id", None)
        if len(buf) >= BATCH:
            upsert(tg, "Document", buf)
            buf = {}
    if buf:
        upsert(tg, "Document", buf)
    print(f"  stored {len(docs)} Document vertices at {EMBED_DIM} dimensions")
    return len(docs)


def build_cases(tg: TigerGraph, g: Gemini, chunk: int = 400) -> int:
    """Embed and store in chunks, so an interrupted run leaves real progress.

    Embedding everything and then upserting means a rate-limit stop after an
    hour writes nothing at all. Chunking persists as it goes, and since the
    embedding cache is keyed on the request, rerunning skips whatever already
    succeeded -- the free tier's per-minute ceiling stops being fatal and
    becomes a pause.
    """
    rows = list(csv.DictReader(open(RAW / "closed_cases_history.csv", encoding="utf-8")))
    print(f"closed cases: {len(rows):,}")
    t0 = time.time()
    n = 0
    for start in range(0, len(rows), chunk):
        part = rows[start:start + chunk]
        embs = g.embed_many([case_text(r) for r in part], progress_every=0)
        buf: dict[str, dict] = {}
        for r, e in zip(part, embs):
            buf[r["case_id"]] = {"emb": e}
            if len(buf) >= BATCH:
                upsert(tg, "ClosedCase", buf)
                buf = {}
        if buf:
            upsert(tg, "ClosedCase", buf)
        n += len(part)
        el = time.time() - t0
        rate = n / el if el else 0
        eta = (len(rows) - n) / rate / 60 if rate else 0
        print(f"\r  {n:,}/{len(rows):,}  {rate:.0f}/s  eta {eta:.0f}m   ",
              end="", flush=True)
    print(f"\r  stored {n:,}/{len(rows):,}" + " " * 24)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--docs", action="store_true")
    ap.add_argument("--cases", action="store_true")
    args = ap.parse_args()
    do_docs = args.docs or not args.cases
    do_cases = args.cases or not args.docs

    tg = TigerGraph()
    g = Gemini()
    print(f"graph : {tg.graph}")
    print(f"embed : {g.embed_model} @ {EMBED_DIM}d\n")

    if do_docs:
        build_docs(tg, g)
        print()
    if do_cases:
        build_cases(tg, g)

    print(f"\nusage: {g.usage}")
    print("\nNext: python src/create_queries.py   (installs vector_search)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
