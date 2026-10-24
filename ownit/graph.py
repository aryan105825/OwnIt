# @exports: Graph(funcs: dict[str, FuncInfo], callees: dict[str, dict[str, int]], callers: dict[str, set[str]], uncertain: dict[str, set[str]], refs: dict[str, set[str]], dynamic: set[str], stats: dict[str, int], by_name: dict[str, list[str]], uncertain_names: set[str]) [dataclass; by_name and uncertain_names are ADDITIVE fields beyond CONTRACTS]
# @exports: build_graph(facts: Facts) -> Graph
# @exports: pool_eligible(g: Graph, fid: str) -> bool
# @exports: reachable(g: Graph, src: str) -> set[str]
# @exports: callers_of(g: Graph, t: str) -> set[str]
# @exports: is_closed(g: Graph, src: str) -> bool
# @exports: is_impact_safe(g: Graph, t: str) -> bool
# @exports: unique_shortest_path(g: Graph, src: str, dst: str) -> list[str]|None
# @exports: shortest_path(g: Graph, src: str, dst: str) -> list[str]|None   # ADDITIVE: any BFS shortest path, deterministic (sorted neighbours)
# @imports: calls.py:DYNAMIC_NAMES
# @imports: model.py:Facts, model.py:FuncInfo
# @imports: scan.py:module_name(relpath: str) -> str
# @schema: callees[f] = {callee id: first call line}; every func id has an entry in callees, callers, uncertain and refs (possibly empty)
# @schema: stats = {functions,total_calls,resolved_calls,uncertain_calls,external_calls,uncertain_functions,dynamic_functions,skipped_files: int}; total_calls = resolved + uncertain + external
# @schema: by_name[simple name] = sorted func ids; uncertain_names = union of all uncertain[*]
# @schema: class constructor calls resolve to Class.__init__ when defined (also via module.Class(...)); a class without __init__ yields no edge and counts as external
# @schema: a self-call to a name with no same-class method (R2b) is uncertain; rule R5 adds the name to uncertain for every attr/self call whose simple name is shared by >=2 project funcs
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .calls import DYNAMIC_NAMES
from .model import Facts, FuncInfo
from .scan import module_name


@dataclass
class Graph:
    funcs: dict[str, FuncInfo]
    callees: dict[str, dict[str, int]]
    callers: dict[str, set[str]]
    uncertain: dict[str, set[str]]
    refs: dict[str, set[str]]
    dynamic: set[str]
    stats: dict[str, int]
    by_name: dict[str, list[str]] = field(default_factory=dict)
    uncertain_names: set[str] = field(default_factory=set)


def build_graph(facts: Facts) -> Graph:
    funcs = facts.funcs
    classes = facts.classes
    imports = facts.imports

    by_name: dict[str, list[str]] = {}
    top: dict[tuple[str, str], str] = {}
    methods: dict[tuple[str, str, str], str] = {}
    for fid in sorted(funcs):
        f = funcs[fid]
        by_name.setdefault(f.name, []).append(fid)
        if f.class_name is None:
            top[(f.module, f.name)] = fid
        else:
            methods[(f.module, f.class_name, f.name)] = fid

    modules: set[str] = {f.module for f in funcs.values()}
    modules |= set(imports)
    modules |= set(classes)
    modules |= {module_name(rp) for rp in facts.sources}

    def resolve_bare(mod: str, name: str) -> tuple[str, object] | None:
        """Bare name -> ("func", fid) | ("class", (module, class)) | None."""
        fid = top.get((mod, name))
        if fid is not None:
            return ("func", fid)
        if name in classes.get(mod, ()):
            return ("class", (mod, name))
        dotted = imports.get(mod, {}).get(name)
        if dotted is not None and "." in dotted:
            m, n = dotted.rsplit(".", 1)
            fid = top.get((m, n))
            if fid is not None:
                return ("func", fid)
            if n in classes.get(m, ()):
                return ("class", (m, n))
        return None

    def resolve_base(mod: str, base: str) -> tuple[str, object] | None:
        """Base name of an attribute call -> ("module", dotted) | ("class", (module, class)) | None."""
        if base in classes.get(mod, ()):
            return ("class", (mod, base))
        dotted = imports.get(mod, {}).get(base)
        if dotted is None:
            return None
        if dotted in modules:
            return ("module", dotted)
        if "." in dotted:
            m, c = dotted.rsplit(".", 1)
            if c in classes.get(m, ()):
                return ("class", (m, c))
        return None

    callees: dict[str, dict[str, int]] = {fid: {} for fid in funcs}
    uncertain: dict[str, set[str]] = {fid: set() for fid in funcs}
    refs: dict[str, set[str]] = {fid: set() for fid in funcs}
    dynamic: set[str] = set(facts.dynamic)
    st = {"total": 0, "resolved": 0, "uncertain": 0, "external": 0}

    for fid in sorted(funcs):
        f = funcs[fid]
        mod = f.module
        out = callees[fid]
        unc = uncertain[fid]

        for rc in facts.calls.get(fid, []):
            st["total"] += 1

            if rc.kind == "name":
                if rc.name in DYNAMIC_NAMES:
                    dynamic.add(fid)  # R7
                    st["external"] += 1
                    continue
                res = resolve_bare(mod, rc.name)  # R1
                if res is not None:
                    if res[0] == "func":
                        target = str(res[1])
                    else:
                        cm, cc = res[1]  # type: ignore[misc]
                        target = methods.get((cm, cc, "__init__"))
                    if target is None:
                        st["external"] += 1
                    else:
                        out.setdefault(target, rc.line)
                        st["resolved"] += 1
                elif rc.name in by_name:
                    unc.add(rc.name)
                    st["uncertain"] += 1
                else:
                    st["external"] += 1
                continue

            # kind "self" or "attr"
            target: str | None = None
            ctor_no_init = False
            r2b = False
            if rc.kind == "self" and f.class_name is not None:
                target = methods.get((mod, f.class_name, rc.name))  # R2
                r2b = target is None  # R2b
            elif rc.base is not None:
                res = resolve_base(mod, rc.base)  # R3
                if res is not None:
                    if res[0] == "module":
                        dotted = str(res[1])
                        target = top.get((dotted, rc.name))
                        if target is None and rc.name in classes.get(dotted, ()):
                            target = methods.get((dotted, rc.name, "__init__"))
                            ctor_no_init = target is None
                    else:
                        cm, cc = res[1]  # type: ignore[misc]
                        target = methods.get((cm, cc, rc.name))

            if len(by_name.get(rc.name, ())) >= 2:
                unc.add(rc.name)  # R5

            if target is not None:
                out.setdefault(target, rc.line)
                st["resolved"] += 1
            elif r2b:
                unc.add(rc.name)
                st["uncertain"] += 1
            elif ctor_no_init:
                st["external"] += 1
            elif rc.name in by_name:  # R4
                unc.add(rc.name)
                st["uncertain"] += 1
            else:
                st["external"] += 1

        for name in sorted(facts.refs.get(fid, ())):  # R6
            res = resolve_bare(mod, name)
            if res is not None:
                if res[0] == "func":
                    refs[fid].add(str(res[1]))
            elif name in by_name:
                unc.add(name)

    callers: dict[str, set[str]] = {fid: set() for fid in funcs}
    for u, targets in callees.items():
        for v in targets:
            callers[v].add(u)

    uncertain_names: set[str] = set()
    for names in uncertain.values():
        uncertain_names |= names

    stats = {
        "functions": len(funcs),
        "total_calls": st["total"],
        "resolved_calls": st["resolved"],
        "uncertain_calls": st["uncertain"],
        "external_calls": st["external"],
        "uncertain_functions": sum(1 for names in uncertain.values() if names),
        "dynamic_functions": len(dynamic),
        "skipped_files": len(facts.skipped),
    }
    return Graph(
        funcs=funcs,
        callees=callees,
        callers=callers,
        uncertain=uncertain,
        refs=refs,
        dynamic=dynamic,
        stats=stats,
        by_name=by_name,
        uncertain_names=uncertain_names,
    )


def pool_eligible(g: Graph, fid: str) -> bool:
    f = g.funcs.get(fid)
    if f is None:
        return False
    if fid in g.dynamic or f.decorated:
        return False
    n = f.name
    return not (n.startswith("__") and n.endswith("__") and len(n) > 4)


def reachable(g: Graph, src: str) -> set[str]:
    seen = {src}
    queue = deque([src])
    while queue:
        u = queue.popleft()
        for v in g.callees.get(u, ()):
            if v not in seen:
                seen.add(v)
                queue.append(v)
    seen.discard(src)
    return seen


def callers_of(g: Graph, t: str) -> set[str]:
    seen = {t}
    queue = deque([t])
    while queue:
        u = queue.popleft()
        for v in g.callers.get(u, ()):
            if v not in seen:
                seen.add(v)
                queue.append(v)
    seen.discard(t)
    return seen


def is_closed(g: Graph, src: str) -> bool:
    for fid in reachable(g, src) | {src}:
        if fid in g.dynamic or g.uncertain.get(fid):
            return False
    return True


def is_impact_safe(g: Graph, t: str) -> bool:
    if not pool_eligible(g, t):
        return False
    name = g.funcs[t].name
    if len(g.by_name.get(name, ())) != 1:
        return False
    if name in g.uncertain_names:
        return False
    for fid in callers_of(g, t) | {t}:
        if fid in g.dynamic:
            return False
    return True


def shortest_path(g: Graph, src: str, dst: str) -> list[str] | None:
    if src == dst or src not in g.funcs or dst not in g.funcs:
        return None
    parent: dict[str, str] = {}
    seen = {src}
    queue = deque([src])
    while queue:
        u = queue.popleft()
        for v in sorted(g.callees.get(u, ())):
            if v in seen:
                continue
            seen.add(v)
            parent[v] = u
            if v == dst:
                path = [dst]
                while path[-1] != src:
                    path.append(parent[path[-1]])
                path.reverse()
                return path
            queue.append(v)
    return None


def unique_shortest_path(g: Graph, src: str, dst: str) -> list[str] | None:
    if src == dst or src not in g.funcs or dst not in g.funcs:
        return None
    dist = {src: 0}
    count = {src: 1}
    parent: dict[str, str] = {}
    queue = deque([src])
    while queue:
        u = queue.popleft()
        for v in sorted(g.callees.get(u, ())):
            if v == u:
                continue
            if v not in dist:
                dist[v] = dist[u] + 1
                count[v] = count[u]
                parent[v] = u
                queue.append(v)
            elif dist[v] == dist[u] + 1:
                count[v] = min(2, count[v] + count[u])
    if dst not in dist or count[dst] != 1:
        return None
    path = [dst]
    while path[-1] != src:
        path.append(parent[path[-1]])
    path.reverse()
    return path
