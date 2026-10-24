# @exports: CHAIN_ENABLED: bool = True
# @exports: generate(g: Graph, facts: Facts, seed: int = 0) -> list[Question]   # FULL set, sorted by id, deterministic
# @exports: select_mode(kind: str) -> "one"|"many"|"order"
# @exports: public_view(q: Question) -> dict   # {id, kind, select, prompt, module, options:[{key,label}]}; no answer, no evidence, no func ids
# @exports: grade(q: Question, answer: list[str]) -> bool
# @imports: graph.py:Graph, graph.py:pool_eligible(g, fid), graph.py:reachable(g, src), graph.py:callers_of(g, t), graph.py:is_closed(g, src), graph.py:is_impact_safe(g, t), graph.py:shortest_path(g, src, dst), graph.py:unique_shortest_path(g, src, dst)
# @imports: model.py:Evidence, model.py:Facts, model.py:Question
# @schema: Question.options items = {"key": "a".., "label": "<qualname> (<relpath>)", "id": "<func id>"}; "id" is ADDITIVE (trace_check needs key->function); public_view strips it
# @schema: callee involved = {S, T}; impact involved = {T} + answers + every function on any answer->T path + each distractor and its reachable set; chain involved = {S} + reachable(S)
# @schema: impact involved ALSO includes intermediate functions on answer->T paths (CONTRACTS formula omitted them, but its INVARIANT requires them)
# @schema: ids: callee sha1("callee|S|T")[:12]; impact sha1("impact|T")[:12]; chain sha1("chain|"+"|".join(path))[:12]
# @schema: options keys: callee a-d, impact a-f, chain one per path node; shuffles use random.Random(f"{seed}:{id}")
from __future__ import annotations

import hashlib
import random

from .graph import (
    Graph,
    callers_of,
    is_closed,
    is_impact_safe,
    pool_eligible,
    reachable,
    shortest_path,
    unique_shortest_path,
)
from .model import Evidence, Facts, Question

CHAIN_ENABLED = True

_KEYS = "abcdef"
_SELECT = {"callee": "one", "impact": "many", "chain": "order"}


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _qual(facts: Facts, fid: str) -> str:
    f = facts.funcs[fid]
    return f"{f.class_name}.{f.name}" if f.class_name else f.name


def _label(facts: Facts, fid: str) -> str:
    return f"{_qual(facts, fid)} ({facts.funcs[fid].relpath})"


def _options(facts: Facts, order: list[str]) -> list[dict[str, str]]:
    return [{"key": _KEYS[i], "label": _label(facts, fid), "id": fid} for i, fid in enumerate(order)]


def _key_of(options: list[dict[str, str]], fid: str) -> str:
    for opt in options:
        if opt["id"] == fid:
            return opt["key"]
    raise KeyError(f"function {fid} is not among the options; this is a bug in questions.py")


def _evidence(facts: Facts, fid: str, line: int, note: str) -> Evidence:
    relpath = facts.funcs[fid].relpath
    lines = facts.sources.get(relpath)
    if lines is None:
        raise KeyError(f"source lines for {relpath} are missing from Facts.sources; rescan the project and try again")
    start = max(1, line - 1)
    end = min(len(lines), line + 1)
    return Evidence(relpath=relpath, start=start, end=end, lines=list(lines[start - 1 : end]), note=note)


def _build(
    facts: Facts,
    qid: str,
    kind: str,
    prompt: str,
    module: str,
    options: list[dict[str, str]],
    answer: list[str],
    evidence: list[Evidence],
    involved: set[str],
) -> Question:
    ordered = sorted(involved)
    return Question(
        id=qid,
        kind=kind,
        prompt=prompt,
        module=module,
        options=options,
        answer=answer,
        evidence=evidence,
        involved=ordered,
        fps={fid: facts.funcs[fid].fingerprint for fid in ordered},
    )


def _gen_callee(g: Graph, facts: Facts, seed: int, pool: list[str], out: dict[str, Question]) -> None:
    for s in sorted(g.funcs):
        if s in g.dynamic:
            continue
        s_uncertain = g.uncertain.get(s, set())
        banned = set(g.callees.get(s, {})) | g.refs.get(s, set()) | {s}
        distract_pool = [d for d in pool if d not in banned and g.funcs[d].name not in s_uncertain]
        if len(distract_pool) < 3:
            continue
        for t, line in sorted(g.callees.get(s, {}).items()):
            if t == s or not pool_eligible(g, t):
                continue
            qid = _sha1(f"callee|{s}|{t}")[:12]
            rng = random.Random(f"{seed}:{qid}")
            order = [t] + rng.sample(distract_pool, 3)
            rng.shuffle(order)
            options = _options(facts, order)
            note = f"`{_qual(facts, s)}` calls `{_qual(facts, t)}` on line {line}."
            out[qid] = _build(
                facts,
                qid,
                "callee",
                f"Which function does `{_qual(facts, s)}` call directly?",
                g.funcs[s].module,
                options,
                [_key_of(options, t)],
                [_evidence(facts, s, line, note)],
                {s, t},
            )


def _gen_impact(g: Graph, facts: Facts, seed: int, pool: list[str], out: dict[str, Question]) -> None:
    reach_cache: dict[str, set[str]] = {}

    def reach(fid: str) -> set[str]:
        if fid not in reach_cache:
            reach_cache[fid] = reachable(g, fid)
        return reach_cache[fid]

    closed = {fid for fid in g.funcs if is_closed(g, fid)}

    for t in sorted(g.funcs):
        if not is_impact_safe(g, t):
            continue
        callers = callers_of(g, t)
        eligible = [x for x in callers if pool_eligible(g, x)]
        if not eligible:
            continue
        answers = sorted(eligible, key=lambda x: _sha1(t + "|" + x))[:4]
        need = 6 - len(answers)
        cands = [d for d in pool if d != t and d not in callers and d in closed and t not in reach(d)]
        if len(cands) < need:
            continue
        qid = _sha1(f"impact|{t}")[:12]
        rng = random.Random(f"{seed}:{qid}")
        distractors = rng.sample(cands, need)
        order = answers + distractors
        rng.shuffle(order)
        options = _options(facts, order)

        involved: set[str] = {t} | set(answers)
        on_path_targets = callers | {t}
        for a in answers:
            involved |= (reach(a) | {a}) & on_path_targets
        for d in distractors:
            involved |= {d} | reach(d)

        evidence: list[Evidence] = []
        qt = _qual(facts, t)
        for a in sorted(answers, key=lambda x: _key_of(options, x)):
            path = shortest_path(g, a, t)
            if path is None:
                raise RuntimeError(f"no call path from {a} to {t} although {a} is a caller; the graph is inconsistent")
            hop = path[1]
            line = g.callees[a][hop]
            if hop == t:
                note = f"`{_qual(facts, a)}` calls `{qt}` directly on line {line}."
            else:
                note = f"`{_qual(facts, a)}` calls `{_qual(facts, hop)}` on line {line}, which leads on to `{qt}`."
            evidence.append(_evidence(facts, a, line, note))

        out[qid] = _build(
            facts,
            qid,
            "impact",
            f"If you change `{qt}`, which of these could break?",
            g.funcs[t].module,
            options,
            [_key_of(options, a) for a in answers],
            evidence,
            involved,
        )


def _gen_chain(g: Graph, facts: Facts, seed: int, out: dict[str, Question]) -> None:
    for s in sorted(g.funcs):
        if s in g.dynamic or not is_closed(g, s):
            continue
        reach_s = reachable(g, s)
        found: list[tuple[str, list[str]]] = []
        for t in sorted(reach_s):
            path = unique_shortest_path(g, s, t)
            if path is None or not 3 <= len(path) <= 5:
                continue
            if not all(pool_eligible(g, n) for n in path[1:]):
                continue
            found.append((_sha1("chain|" + "|".join(path)), path))
        found.sort(key=lambda item: item[0])
        for _, path in found[:2]:
            qid = _sha1("chain|" + "|".join(path))[:12]
            rng = random.Random(f"{seed}:{qid}")
            order = list(path)
            for _attempt in range(50):
                rng.shuffle(order)
                if order != path:
                    break
            if order == path:
                order = path[1:] + path[:1]
            options = _options(facts, order)
            evidence = []
            for u, v in zip(path, path[1:]):
                line = g.callees[u][v]
                note = f"`{_qual(facts, u)}` calls `{_qual(facts, v)}` on line {line}."
                evidence.append(_evidence(facts, u, line, note))
            out[qid] = _build(
                facts,
                qid,
                "chain",
                f"Put the shortest call chain from `{_qual(facts, path[0])}` to `{_qual(facts, path[-1])}` in order.",
                g.funcs[s].module,
                options,
                [_key_of(options, n) for n in path],
                evidence,
                {s} | reach_s,
            )


def generate(g: Graph, facts: Facts, seed: int = 0) -> list[Question]:
    out: dict[str, Question] = {}
    pool = sorted(fid for fid in g.funcs if pool_eligible(g, fid))
    _gen_callee(g, facts, seed, pool, out)
    _gen_impact(g, facts, seed, pool, out)
    if CHAIN_ENABLED:
        _gen_chain(g, facts, seed, out)
    return [out[qid] for qid in sorted(out)]


def select_mode(kind: str) -> str:
    try:
        return _SELECT[kind]
    except KeyError:
        raise ValueError(f"unknown question kind {kind!r}; expected one of {sorted(_SELECT)}") from None


def public_view(q: Question) -> dict:
    return {
        "id": q.id,
        "kind": q.kind,
        "select": select_mode(q.kind),
        "prompt": q.prompt,
        "module": q.module,
        "options": [{"key": o["key"], "label": o["label"]} for o in q.options],
    }


def grade(q: Question, answer: list[str]) -> bool:
    mode = select_mode(q.kind)
    if mode == "one":
        return len(answer) == 1 and answer[0] == q.answer[0]
    if mode == "many":
        return len(set(answer)) == len(answer) and set(answer) == set(q.answer)
    return list(answer) == list(q.answer)
