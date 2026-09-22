"""Run the agent over the case pack and write cases/HHG-0NN.json.

    python src/run_cases.py --all
    python src/run_cases.py --case HHG-014
    python src/run_cases.py --all --fresh     # ignore the LLM cache

Cases are processed in `opened_at` order, not pack order. HHG-014 -- the ring
case -- opens fourth, so the sixteen investigations after it can retrieve it
from the graph and cite it. Running the twenty in parallel would be faster and
would leave nothing to show for the case-memory criterion; the cost of doing
it properly is one sorted() call.

Each case is written as soon as it completes, so a run interrupted by an
auto-suspend or a rate limit keeps everything finished so far.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import Investigator, RingIndex, Trigger  # noqa: E402
from bootstrap import chronological, load_pack, validate_all  # noqa: E402
from llm import Gemini  # noqa: E402
from schema import Answer  # noqa: E402
from tools import FraudTools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--case", action="append", help="run specific case ids")
    ap.add_argument("--out", type=Path, default=ROOT / "cases")
    ap.add_argument("--pack", type=Path, default=ROOT / "data" / "raw" / "case_pack.csv")
    ap.add_argument("--fresh", action="store_true", help="bypass the LLM cache")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-mcp", action="store_true",
                    help="call the graph directly instead of through the MCP server (debug)")
    args = ap.parse_args()

    pack = load_pack(args.pack)
    rows = chronological(pack)
    if args.case:
        wanted = set(args.case)
        rows = [r for r in rows if r["case_id"] in wanted]
    elif not args.all:
        ap.print_help()
        return 1

    # By default the agent reaches the graph THROUGH the MCP server (required):
    # McpTools launches src/mcp_server.py over stdio and calls its tools.
    if args.no_mcp:
        tools = FraudTools()
        print("graph access: DIRECT (--no-mcp)")
    else:
        from mcp_tools import McpTools
        print("graph access: via MCP server (launching stdio subprocess) ...", flush=True)
        tools = McpTools()
    llm = Gemini(cache=not args.fresh)

    # Warm up the graph before the real work: a Savanna workspace waking from
    # auto-suspend refuses the first call or two, and the first heavy op (the
    # ring scan) shouldn't be what absorbs that. Retry a cheap read until the
    # graph answers, so case 1 never fails on a cold workspace -- important for
    # a live demo.
    for attempt in range(6):
        try:
            tools.get_transaction(rows[0]["flagged_txn_id"])
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == 5:
                print(f"  graph not responding after warm-up retries: {str(exc)[:120]}")
                break
            print(f"  waiting for the graph to wake (attempt {attempt + 1}/6) ...", flush=True)
            time.sleep(15)

    print("building the ring index (one portfolio-wide scan) ...", flush=True)
    t0 = time.time()
    rings = RingIndex(tools)
    print(f"  {len(rings.clusters)} cluster(s) above the ring-score threshold "
          f"in {time.time() - t0:,.0f}s")
    for c in rings.clusters[:5]:
        print(f"    score {c['ring_score']:>6.1f}  {c['n_cards']:>3} cards  "
              f"new={c['new_ratio']:.2f} anon={c['anon_ratio']:.2f}  {c['profile'][:56]}")

    agent = Investigator(tools=tools, llm=llm, rings=rings, verbose=not args.quiet)

    ok, failed = 0, []
    for i, row in enumerate(rows, 1):
        trigger = Trigger.from_row(row)
        print(f"\n[{i}/{len(rows)}] {trigger.case_id}  ({trigger.trigger_type})", flush=True)
        try:
            answer = agent.investigate(trigger)
            answer.write(args.out / f"{trigger.case_id}.json")
            print(f"    -> {answer.case.verdict}/{answer.case.pattern} "
                  f"p={answer.case.fraud_probability} "
                  f"${answer.case.exposure_usd:,.2f} sar={answer.sar.file} "
                  f"({answer.tool_calls} calls, {answer.tokens:,} tokens, "
                  f"{answer.latency_s}s)")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED: {exc}")
            traceback.print_exc(limit=3)
            failed.append((trigger.case_id, str(exc)))

    if hasattr(tools, "close"):
        tools.close()  # tear down the MCP subprocess

    print(f"\n{ok}/{len(rows)} case(s) completed")
    if failed:
        for cid, err in failed:
            print(f"  {cid}: {err[:120]}")

    print("\nvalidating cases/ ...")
    problems = validate_all(args.out, ROOT / "data", pack)
    if problems:
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"{len(pack)} files. All schema-valid, all invariants satisfied. 0 problems.")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
