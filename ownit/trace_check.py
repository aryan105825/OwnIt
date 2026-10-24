# @exports: main(argv: list[str]|None = None) -> int   # python -m ownit.trace_check --corpus ownit|textwrap --out FILE; exit 1 on any gate failure
# @imports: scan.py:scan_path(path: str) -> Facts
# @imports: graph.py:Graph, graph.py:build_graph(facts), graph.py:pool_eligible(g, fid)
# @imports: questions.py:generate(g, facts, seed), questions.py:grade(q, answer), questions.py:public_view(q)
# @imports: state.py:load_state(path), state.py:save_state(state, path), state.py:record_answer(state, q, correct), state.py:classify(state, questions), state.py:orphans(state, questions), state.py:ownership(questions, statuses)
# @imports: model.py:Facts, model.py:Question
# @schema: JSON {corpus, observed_edges, unattributed, unattributed_ratio, static_edges, observed_in_static_pct, violations:[{caller,callee}], violation_count, callee_questions, impact_questions, contradicted_questions}
# @schema: observed_edges = DISTINCT (caller id, callee id) pairs seen by sys.setprofile "call" events where both are project functions; <listcomp>/<dictcomp>/<setcomp> caller frames are skipped through
# @schema: unattributed = DISTINCT (caller code location, callee id) pairs whose caller frame is a project file but not a mapped function (nested def, lambda, genexpr, <module>); callers in non-project files are ignored entirely
# @schema: workload "ownit": scan own package, generate, record/classify/save/load state; workload "textwrap": textwrap.wrap/fill/shorten/dedent/indent over README.md; corpus "textwrap" = the stdlib textwrap.py source
# @schema: contradicted questions: callee distractor observed as a callee of S; impact distractor with an observed-edge path to T (T recovered as the involved id whose sha1("impact|"+id)[:12] equals the question id)
# @schema: exit 1 if violation_count>0, contradicted>0, unattributed/(observed+unattributed)>0.25, or observed_edges < 15 (ownit) / 5 (textwrap)
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import textwrap
from collections import deque
from typing import Any

from .graph import Graph, build_graph, pool_eligible
from .model import Facts, Question
from .questions import generate, grade, public_view
from .scan import scan_path
from .state import classify, load_state, orphans, ownership, record_answer, save_state

_TRANSPARENT = ("<listcomp>", "<dictcomp>", "<setcomp>")
_MIN_EDGES = {"ownit": 15, "textwrap": 5}
_MAX_UNATTRIBUTED_RATIO = 0.25


class _Recorder:
    def __init__(self, facts: Facts) -> None:
        self._real: dict[str, str] = {}
        self.func_map: dict[tuple[str, int], str] = {}
        self.project_files: set[str] = set()
        for relpath in facts.sources:
            self.project_files.add(self._norm(os.path.join(facts.root, *relpath.split("/"))))
        for fid, info in facts.funcs.items():
            path = self._norm(os.path.join(facts.root, *info.relpath.split("/")))
            self.func_map[(path, info.first_line)] = fid
        self.edges: set[tuple[str, str]] = set()
        self.unattributed: set[tuple[str, str]] = set()

    def _norm(self, filename: str) -> str:
        real = self._real.get(filename)
        if real is None:
            real = os.path.realpath(filename)
            self._real[filename] = real
        return real

    def record(self, frame: Any, event: str, arg: Any) -> None:
        if event != "call":
            return
        code = frame.f_code
        callee = self.func_map.get((self._norm(code.co_filename), code.co_firstlineno))
        if callee is None:
            return
        caller_frame = frame.f_back
        while caller_frame is not None and caller_frame.f_code.co_name in _TRANSPARENT:
            caller_frame = caller_frame.f_back
        if caller_frame is None:
            return
        caller_code = caller_frame.f_code
        caller_file = self._norm(caller_code.co_filename)
        caller = self.func_map.get((caller_file, caller_code.co_firstlineno))
        if caller is not None:
            self.edges.add((caller, callee))
        elif caller_file in self.project_files:
            where = f"{caller_file}:{caller_code.co_name}:{caller_code.co_firstlineno}"
            self.unattributed.add((where, callee))


def run_ownit_workload() -> None:
    package_dir = os.path.dirname(os.path.abspath(__file__))
    facts = scan_path(package_dir)
    graph = build_graph(facts)
    questions = generate(graph, facts, seed=0)
    if not questions:
        raise RuntimeError("the ownit workload produced no questions; scan the ownit package and check ownit/questions.py")
    with tempfile.TemporaryDirectory() as scratch:
        state_path = os.path.join(scratch, "state.json")
        state = load_state(state_path)
        for question in questions[:25]:
            record_answer(state, question, grade(question, question.answer))
        save_state(state, state_path)
        reloaded = load_state(state_path)
        statuses = classify(reloaded, questions)
        leftovers = orphans(reloaded, questions)
        summary = ownership(questions, statuses)
        views = [public_view(question) for question in questions]
    if leftovers or summary["overall"]["total"] != len(questions) or len(views) != len(questions):
        raise RuntimeError("the ownit workload state round-trip is inconsistent; check ownit/state.py")


def run_textwrap_workload(readme_path: str) -> None:
    if not os.path.exists(readme_path):
        raise FileNotFoundError(f"{readme_path} is missing; the textwrap workload needs the repo README.md as input text")
    with open(readme_path, encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        raise RuntimeError(f"{readme_path} is empty; the textwrap workload needs real text")
    paragraphs = [part for part in text.split("\n\n") if part.strip()]
    results = []
    for width in (40, 72):
        results.append(textwrap.wrap(text, width=width))
        results.append(textwrap.fill(text, width=width))
    results.append(textwrap.shorten(text, width=60))
    results.append(textwrap.dedent("    " + "\n    ".join(paragraphs[:3])))
    results.append(textwrap.indent(text, "> "))
    if not all(results):
        raise RuntimeError("a textwrap call returned an empty result; check the README.md input")


def _profiled_run(corpus: str, recorder: _Recorder, readme_path: str) -> None:
    sys.setprofile(recorder.record)
    try:
        if corpus == "ownit":
            run_ownit_workload()
        else:
            run_textwrap_workload(readme_path)
    finally:
        sys.setprofile(None)


def _impact_target(question: Question) -> str:
    for fid in question.involved:
        if hashlib.sha1(f"impact|{fid}".encode("utf-8")).hexdigest()[:12] == question.id:
            return fid
    raise RuntimeError(f"cannot recover the target function of impact question {question.id}; check ownit/questions.py ids")


def _contradicted(questions: list[Question], edges: set[tuple[str, str]]) -> int:
    adjacency: dict[str, set[str]] = {}
    for u, v in edges:
        adjacency.setdefault(u, set()).add(v)
    bad: set[str] = set()
    for question in questions:
        answer_ids = {opt["id"] for opt in question.options if opt["key"] in question.answer}
        distractors = [opt["id"] for opt in question.options if opt["id"] not in answer_ids]
        if question.kind == "callee":
            sources = [fid for fid in question.involved if fid not in answer_ids]
            if len(sources) != 1:
                raise RuntimeError(f"callee question {question.id} does not have exactly one source function")
            if any(d in adjacency.get(sources[0], set()) for d in distractors):
                bad.add(question.id)
        elif question.kind == "impact":
            target = _impact_target(question)
            for d in distractors:
                seen = {d}
                queue = deque([d])
                while queue:
                    node = queue.popleft()
                    for nxt in adjacency.get(node, ()):
                        if nxt not in seen:
                            seen.add(nxt)
                            queue.append(nxt)
                if target in seen:
                    bad.add(question.id)
                    break
    return len(bad)


def _analyse(corpus: str, graph: Graph, questions: list[Question], recorder: _Recorder) -> dict:
    edges = sorted(recorder.edges)
    in_static = sum(1 for u, v in edges if v in graph.callees.get(u, {}))
    violations = []
    for u, v in edges:
        if not pool_eligible(graph, v) or u in graph.dynamic:
            continue
        if v in graph.callees.get(u, {}) or v in graph.refs.get(u, set()):
            continue
        if graph.funcs[v].name in graph.uncertain.get(u, set()):
            continue
        violations.append({"caller": u, "callee": v})
    observed = len(edges)
    unattributed = len(recorder.unattributed)
    total = observed + unattributed
    return {
        "corpus": corpus,
        "observed_edges": observed,
        "unattributed": unattributed,
        "unattributed_ratio": round(unattributed / total, 4) if total else 0.0,
        "static_edges": sum(len(targets) for targets in graph.callees.values()),
        "observed_in_static_pct": round(100.0 * in_static / observed, 1) if observed else 0.0,
        "violations": violations,
        "violation_count": len(violations),
        "callee_questions": sum(1 for q in questions if q.kind == "callee"),
        "impact_questions": sum(1 for q in questions if q.kind == "impact"),
        "contradicted_questions": _contradicted(questions, recorder.edges),
    }


def _corpus_path(corpus: str) -> str:
    if corpus == "ownit":
        return os.path.dirname(os.path.abspath(__file__))
    path = textwrap.__file__
    if not path or not path.endswith(".py"):
        raise RuntimeError(f"cannot find the textwrap source file (got {path!r}); use a CPython install that ships .py sources")
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ownit.trace_check", description="Check static answers against runtime call traces.")
    parser.add_argument("--corpus", required=True, choices=["ownit", "textwrap"], help="which code base to trace")
    parser.add_argument("--out", required=True, help="where to write the JSON result")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    facts = scan_path(_corpus_path(args.corpus))
    if not facts.funcs:
        raise RuntimeError(f"no functions found in the {args.corpus} corpus; nothing to check")
    graph = build_graph(facts)
    questions = generate(graph, facts, seed=0)
    recorder = _Recorder(facts)
    readme_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
    _profiled_run(args.corpus, recorder, readme_path)
    result = _analyse(args.corpus, graph, questions, recorder)

    directory = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(directory, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True, ensure_ascii=False, indent=1)
        handle.write("\n")

    print(
        f"{args.corpus}: observed_edges={result['observed_edges']} unattributed={result['unattributed']} "
        f"violations={result['violation_count']} contradicted={result['contradicted_questions']} "
        f"observed_in_static_pct={result['observed_in_static_pct']}"
    )
    failures = []
    if result["violation_count"] > 0:
        failures.append(
            f"{result['violation_count']} runtime call(s) the static graph did not predict (see 'violations' in {args.out}); "
            "fix the resolution rules in ownit/graph.py or exclude the pattern from questions"
        )
    if result["contradicted_questions"] > 0:
        failures.append(f"{result['contradicted_questions']} question(s) contradicted by runtime traces; a distractor was observed to be a real callee or caller")
    total = result["observed_edges"] + result["unattributed"]
    if total and result["unattributed"] / total > _MAX_UNATTRIBUTED_RATIO:
        failures.append(f"{result['unattributed']} of {total} traced calls could not be attributed to a function (limit 25%)")
    if result["observed_edges"] < _MIN_EDGES[args.corpus]:
        failures.append(f"only {result['observed_edges']} edges observed; need at least {_MIN_EDGES[args.corpus]} for a meaningful check")
    for message in failures:
        print(f"FAIL {args.corpus}: {message}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
