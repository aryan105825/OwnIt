# @exports: main(argv: list[str]|None = None) -> int   # python -m ownit.mutate_check --out FILE [--n 10] [--seed 0]; exit 1 unless precision == recall == 1.0 and format_only_stale == 0
# @imports: scan.py:scan_path(path: str) -> Facts
# @imports: graph.py:build_graph(facts) -> Graph
# @imports: questions.py:generate(g, facts, seed) -> list[Question]
# @imports: state.py:load_state(path), state.py:record_answer(state, q, correct), state.py:classify(state, questions), state.py:orphans(state, questions)
# @imports: model.py:FuncInfo
# @schema: JSON {n, mutations:[{kind: "body"|"format"|"rename", func: id, expected: [qid], measured: [qid]}], precision, recall, format_only_stale: int, rename_orphaned_pct: float}
# @schema: n = number of BODY mutations actually run (--n, or fewer if the package has fewer mutable functions); format = 3 mutations, rename = 3 mutations (fewer if too few candidates)
# @schema: expected = ids of questions whose `involved` contains the mutated function; body measured = stale ids; format measured = stale ids (must be empty); rename measured = orphaned ids (reported, not gated)
# @schema: precision/recall are pooled over BODY mutations (true positives / measured, true positives / expected); a mutation targets only functions that appear in some question's involved
# @schema: body mutation appends `_own_mut = 0` as the last body statement; format mutation inserts one blank line and one comment after the function; rename mutation renames the def to <name>_renamed
from __future__ import annotations

import argparse
import ast
import json
import os
import random
import shutil
import sys
import tempfile

from .graph import build_graph
from .model import FuncInfo
from .questions import generate
from .scan import scan_path
from .state import classify, load_state, orphans, record_answer

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _read_lines(path: str) -> tuple[str, list[str]]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        text = handle.read()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return text, lines


def _find_node(tree: ast.Module, info: FuncInfo) -> ast.FunctionDef | ast.AsyncFunctionDef:
    candidates: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if isinstance(node, _FUNC_TYPES) and info.class_name is None:
            candidates.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == info.class_name:
            candidates.extend(sub for sub in node.body if isinstance(sub, _FUNC_TYPES))
    for node in candidates:
        first = min([d.lineno for d in node.decorator_list] + [node.lineno])
        if node.name == info.name and first == info.first_line and node.end_lineno == info.end_line:
            return node
    raise RuntimeError(f"could not locate {info.id} in {info.relpath}; rescan and try again")


def _is_mutable(src: str, info: FuncInfo, trees: dict[str, ast.Module]) -> bool:
    tree = trees.get(info.relpath)
    if tree is None:
        text, _ = _read_lines(os.path.join(src, *info.relpath.split("/")))
        tree = ast.parse(text)
        trees[info.relpath] = tree
    node = _find_node(tree, info)
    return node.body[0].lineno != node.lineno


def _apply(kind: str, target_dir: str, info: FuncInfo) -> None:
    path = os.path.join(target_dir, *info.relpath.split("/"))
    text, lines = _read_lines(path)
    node = _find_node(ast.parse(text), info)
    if node.body[0].lineno == node.lineno:
        raise RuntimeError(f"{info.id} has its body on the def line; it cannot be mutated safely")
    indent = " " * node.body[0].col_offset
    if kind == "body":
        lines.insert(info.end_line, indent + "_own_mut = 0")
    elif kind == "format":
        lines.insert(info.end_line, "")
        lines.insert(info.end_line + 1, indent + "# formatting-only edit")
    elif kind == "rename":
        index = node.lineno - 1
        old = f"def {info.name}("
        if old not in lines[index]:
            raise RuntimeError(f"cannot rename {info.id}: expected {old!r} on line {node.lineno} of {info.relpath}")
        lines[index] = lines[index].replace(old, f"def {info.name}_renamed(", 1)
    else:
        raise ValueError(f"unknown mutation kind {kind!r}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _run_mutation(src: str, kind: str, info: FuncInfo, base_state: dict, seed: int) -> list[str]:
    with tempfile.TemporaryDirectory() as scratch:
        target = os.path.join(scratch, "ownit")
        shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        _apply(kind, target, info)
        facts = scan_path(target)
        graph = build_graph(facts)
        questions = generate(graph, facts, seed=seed)
        if kind == "rename":
            return orphans(base_state, questions)
        statuses = classify(base_state, questions)
        return sorted(qid for qid, entry in statuses.items() if entry["status"] == "stale")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ownit.mutate_check", description="Check that questions go stale exactly when their code changes.")
    parser.add_argument("--out", required=True, help="where to write the JSON result")
    parser.add_argument("--n", type=int, default=10, help="number of body mutations (default 10)")
    parser.add_argument("--seed", type=int, default=0, help="seed for question generation and mutation choice")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    src = os.path.dirname(os.path.abspath(__file__))

    base_facts = scan_path(src)
    base_graph = build_graph(base_facts)
    base_questions = generate(base_graph, base_facts, seed=args.seed)
    if not base_questions:
        raise RuntimeError("the ownit package produced no questions; run `python -m ownit.cli scan ownit` to investigate")

    with tempfile.TemporaryDirectory() as scratch:
        state = load_state(os.path.join(scratch, "state.json"))
    for question in base_questions:
        record_answer(state, question, True)
    baseline = classify(state, base_questions)
    not_owned = sorted(qid for qid, entry in baseline.items() if entry["status"] != "owned")
    if not_owned:
        raise RuntimeError(f"baseline is not all owned after answering every question correctly ({len(not_owned)} differ); check ownit/state.py classify")

    expected_by_func: dict[str, list[str]] = {}
    for question in base_questions:
        for fid in question.involved:
            expected_by_func.setdefault(fid, []).append(question.id)

    rng = random.Random(args.seed)
    order = sorted(expected_by_func)
    rng.shuffle(order)
    trees: dict[str, ast.Module] = {}
    mutable = [fid for fid in order if _is_mutable(src, base_facts.funcs[fid], trees)]
    body_funcs = mutable[: args.n]
    rest = mutable[args.n :]
    format_funcs = rest[:3]
    rename_funcs = [fid for fid in rest[3:] if not base_facts.funcs[fid].name.startswith("__")][:3]
    if not body_funcs:
        raise RuntimeError("no function is involved in any question, so there is nothing to mutate; scan a larger code base")

    mutations: list[dict] = []
    for kind, funcs in (("body", body_funcs), ("format", format_funcs), ("rename", rename_funcs)):
        for fid in funcs:
            measured = _run_mutation(src, kind, base_facts.funcs[fid], state, args.seed)
            mutations.append({"kind": kind, "func": fid, "expected": sorted(expected_by_func[fid]), "measured": sorted(measured)})

    true_pos = measured_total = expected_total = 0
    for item in mutations:
        if item["kind"] != "body":
            continue
        expected = set(item["expected"])
        measured = set(item["measured"])
        true_pos += len(expected & measured)
        measured_total += len(measured)
        expected_total += len(expected)
    precision = true_pos / measured_total if measured_total else 1.0
    recall = true_pos / expected_total if expected_total else 1.0
    format_only_stale = sum(len(item["measured"]) for item in mutations if item["kind"] == "format")
    rename_hits = sum(len(set(item["expected"]) & set(item["measured"])) for item in mutations if item["kind"] == "rename")
    rename_total = sum(len(item["expected"]) for item in mutations if item["kind"] == "rename")
    rename_pct = round(100.0 * rename_hits / rename_total, 1) if rename_total else 0.0

    result = {
        "n": len(body_funcs),
        "mutations": mutations,
        "precision": precision,
        "recall": recall,
        "format_only_stale": format_only_stale,
        "rename_orphaned_pct": rename_pct,
    }
    directory = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(directory, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True, ensure_ascii=False, indent=1)
        handle.write("\n")

    print(
        f"body mutations={len(body_funcs)} precision={precision} recall={recall} "
        f"format_only_stale={format_only_stale} rename_orphaned_pct={rename_pct}"
    )
    if len(body_funcs) < args.n:
        print(f"note: only {len(body_funcs)} mutable functions were available for {args.n} requested body mutations", file=sys.stderr)
    failures = []
    if precision != 1.0 or recall != 1.0:
        failures.append(f"staleness precision={precision} recall={recall}; both must be 1.0 (see 'mutations' in {args.out} for the differing question ids)")
    if format_only_stale != 0:
        failures.append(f"{format_only_stale} question(s) went stale after formatting-only edits; check fingerprint() in ownit/scan.py")
    for message in failures:
        print(f"FAIL mutate_check: {message}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
