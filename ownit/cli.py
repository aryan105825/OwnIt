# @exports: main(argv: list[str]|None = None) -> int   # python -m ownit.cli scan PATH [--json OUT] [--seed 0]; exit 0 ok, 2 if PATH is bad or has 0 functions
# @imports: scan.py:scan_path(path: str) -> Facts
# @imports: graph.py:build_graph(facts: Facts) -> Graph
# @imports: questions.py:generate(g: Graph, facts: Facts, seed: int = 0) -> list[Question]
# @imports: model.py:to_json(obj) -> dict|list|scalar
# @schema: --json OUT writes {"stats": {...graph stats..., "questions": int}, "questions": [full Question incl. answer, evidence, involved, fps]}
# @schema: stdout lists graph stats and question counts per kind; skipped files are named on stderr
from __future__ import annotations

import argparse
import json
import os
import sys

from .graph import build_graph
from .model import to_json
from .questions import generate
from .scan import scan_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ownit.cli", description="OwnIt command line tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="scan a Python project and print call-graph statistics and question counts")
    scan.add_argument("path", help="directory or .py file to scan")
    scan.add_argument("--json", dest="json_out", default=None, help="write stats and all questions (with answers) to this JSON file")
    scan.add_argument("--seed", type=int, default=0, help="seed for option shuffling (default 0)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        facts = scan_path(args.path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if facts.skipped:
        print(
            f"warning: skipped {len(facts.skipped)} file(s) that could not be parsed: " + ", ".join(facts.skipped),
            file=sys.stderr,
        )
    if not facts.funcs:
        print(
            f"error: no functions found in {args.path!r}. Point the scan at a directory or file that contains Python functions.",
            file=sys.stderr,
        )
        return 2

    graph = build_graph(facts)
    questions = generate(graph, facts, seed=args.seed)

    stats = dict(graph.stats)
    stats["questions"] = len(questions)

    print(f"OwnIt scan of {facts.root}")
    for key in sorted(stats):
        print(f"{key}: {stats[key]}")
    for kind in ("callee", "impact", "chain"):
        print(f"{kind}_questions: {sum(1 for q in questions if q.kind == kind)}")

    if args.json_out:
        payload = {"stats": stats, "questions": [to_json(q) for q in questions]}
        directory = os.path.dirname(os.path.abspath(args.json_out))
        os.makedirs(directory, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, ensure_ascii=False, indent=1)
            fh.write("\n")
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
