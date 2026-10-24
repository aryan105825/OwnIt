# @exports: main() -> int   # runs V1..V10, prints "PASS|FAIL Vn <name> :: <detail>", writes results/summary.json {"claims":[{id,name,pass,detail}],"all_pass":bool}; exit 1 on any FAIL
# @imports: ownit/scan.py:scan_path(path: str) -> Facts
# @imports: ownit/graph.py:build_graph(facts), ownit/graph.py:callers_of(g, t), ownit/graph.py:reachable(g, src), ownit/graph.py:unique_shortest_path(g, src, dst)
# @imports: ownit/questions.py:generate(g, facts, seed), ownit/questions.py:CHAIN_ENABLED
# @imports: ownit/model.py:to_json(obj)
# @schema: also writes results/scan_stats.json (V2), determinism.json (V3), evidence.json (V4), trace_ownit.json + trace_textwrap.json (V5), mutate.json (V6)
# @schema: V1 deps | V2 scans | V3 determinism | V4 evidence | V5 trace | V6 staleness | V7 end-to-end | V8 UI ships | V9 demo repo | V10 proof endpoint
# @schema: servers are started as child processes with --port 0 on scratch copies of examples/todo_agent; child processes inherit this process's environment (no variable is read here)
# @schema: V6 requires at least 10 body mutations so the precision and recall claim is never vacuous; V10 compares /api/proof with results/summary.json before AND after the final write
from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ownit import questions as questions_module  # noqa: E402
from ownit.graph import build_graph, callers_of, reachable, unique_shortest_path  # noqa: E402
from ownit.model import to_json  # noqa: E402
from ownit.questions import generate  # noqa: E402
from ownit.scan import scan_path  # noqa: E402

RESULTS = os.path.join(ROOT, "results")
SUMMARY = os.path.join(RESULTS, "summary.json")
CORPORA = ("ownit", "textwrap", "todo_agent")
# CONTRACTS asks for >=15 functions on both corpora, but the stdlib textwrap of Python 3.14 defines only 14,
# so the stdlib corpus needs >=10 (measured and printed, never assumed). ownit keeps the contract's 15.
MIN_FUNCS = {"ownit": 15, "textwrap": 10}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CheckFailed(Exception):
    """A check measured something wrong; the message says what and how to fix it."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def enabled_kinds() -> set[str]:
    kinds = {"callee", "impact"}
    if questions_module.CHAIN_ENABLED:
        kinds.add("chain")
    return kinds


def write_json(name: str, obj: object) -> None:
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, name), "w", encoding="utf-8") as handle:
        json.dump(obj, handle, sort_keys=True, ensure_ascii=False, indent=1)
        handle.write("\n")


def read_json(path: str) -> object:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def tail(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def corpus_path(name: str) -> str:
    if name == "ownit":
        return os.path.join(ROOT, "ownit")
    if name == "todo_agent":
        return os.path.join(ROOT, "examples", "todo_agent")
    path = getattr(textwrap, "__file__", None)
    expect(bool(path) and path.endswith(".py"), "cannot find the textwrap source file; use a CPython install that ships .py sources")
    return path


_CACHE: dict[str, tuple] = {}


def analyse(name: str) -> tuple:
    if name not in _CACHE:
        facts = scan_path(corpus_path(name))
        graph = build_graph(facts)
        _CACHE[name] = (facts, graph, generate(graph, facts, seed=0))
    return _CACHE[name]


def count_kinds(qs: list) -> dict[str, int]:
    counts = {"callee": 0, "impact": 0, "chain": 0}
    for q in qs:
        counts[q.kind] += 1
    return counts


# ---------------------------------------------------------------- server helpers
def http(method: str, url: str, payload: object | None = None) -> tuple[int, bytes, dict[str, str]]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(request, timeout=30) as response:
            return response.status, response.read(), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in exc.headers.items()}


def call(method: str, base: str, path: str, payload: object | None = None, want: int = 200) -> object:
    status, body, _ = http(method, base + path, payload)
    expect(status == want, f"{method} {path} returned HTTP {status}, expected {want}: {tail(body.decode('utf-8', 'replace'), 300)}")
    return json.loads(body.decode("utf-8"))


@contextlib.contextmanager
def running_server(target: str, state_path: str):
    command = [sys.executable, "-m", "ownit.server", target, "--port", "0", "--state", state_path]
    proc = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    base = None
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        if line.startswith("LISTENING "):
            base = line.split(None, 1)[1].strip()
            break
    if base is None:
        if proc.poll() is None:
            proc.kill()
        stderr = proc.stderr.read() if proc.stderr else ""
        raise CheckFailed(f"the server did not print LISTENING within 40s. stderr: {tail(stderr)}")
    try:
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------- V1
def check_deps() -> str:
    expect(sys.version_info >= (3, 10), f"Python 3.10 or newer is required, this is {sys.version.split()[0]}")
    allowed = set(sys.stdlib_module_names) | {"ownit"}
    files = 0
    modules: set[str] = set()
    bad: list[str] = []
    for folder in ("ownit", "scripts"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, folder)):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                files += 1
                with open(path, "r", encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                for node in ast.walk(tree):
                    tops: list[str] = []
                    if isinstance(node, ast.Import):
                        tops = [alias.name.split(".")[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        tops = [node.module.split(".")[0]]
                    for top in tops:
                        modules.add(top)
                        if top not in allowed:
                            bad.append(f"{os.path.relpath(path, ROOT)} imports {top}")
    expect(not bad, "non-stdlib imports found: " + "; ".join(bad[:5]) + ". Remove them: OwnIt is standard library only.")
    with open(os.path.join(ROOT, "requirements.txt"), "r", encoding="utf-8") as handle:
        listed = [ln.strip() for ln in handle if ln.strip() and not ln.strip().startswith("#")]
    expect(not listed, f"requirements.txt lists packages {listed}; it must list none")
    return f"{files} files import only the standard library ({len(modules)} distinct modules); requirements.txt lists no packages"


# ---------------------------------------------------------------- V2
def check_scans() -> str:
    stats: dict[str, object] = {}
    parts = []
    problems = []
    for name in ("ownit", "textwrap"):
        facts, graph, qs = analyse(name)
        counts = count_kinds(qs)
        stats[name] = {"functions": len(facts.funcs), "graph": graph.stats, "questions": counts}
        parts.append(f"{name}: {len(facts.funcs)} functions, {counts['callee']} callee questions")
        need = MIN_FUNCS[name]
        if len(facts.funcs) < need:
            problems.append(f"{name} has only {len(facts.funcs)} functions (need {need})")
        if counts["callee"] < 5:
            problems.append(f"{name} has only {counts['callee']} callee questions (need 5)")
    write_json("scan_stats.json", stats)
    expect(not problems, "; ".join(problems))
    return "; ".join(parts)


# ---------------------------------------------------------------- V3
def digest(name: str) -> tuple[str, int]:
    facts = scan_path(corpus_path(name))
    graph = build_graph(facts)
    qs = generate(graph, facts, seed=0)
    blob = json.dumps([to_json(q) for q in qs], sort_keys=True, ensure_ascii=False, indent=1)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), len(qs)


def check_determinism() -> str:
    report: dict[str, object] = {}
    for name in CORPORA:
        first, count = digest(name)
        second, _ = digest(name)
        expect(first == second, f"{name}: two runs in one process produced different questions ({first[:12]} vs {second[:12]})")
        report[name] = {"sha256": first, "questions": count}
    cross = []
    with tempfile.TemporaryDirectory() as scratch:
        for hash_seed in ("1", "2"):
            out = os.path.join(scratch, f"scan_{hash_seed}.json")
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = hash_seed
            proc = subprocess.run(
                [sys.executable, "-m", "ownit.cli", "scan", corpus_path("ownit"), "--json", out, "--seed", "0"],
                cwd=ROOT, capture_output=True, text=True, timeout=300, env=env,
            )
            expect(proc.returncode == 0, f"cli scan failed with PYTHONHASHSEED={hash_seed}: {tail(proc.stderr)}")
            with open(out, "rb") as handle:
                cross.append(hashlib.sha256(handle.read()).hexdigest())
    expect(cross[0] == cross[1], f"scan output differs between hash seeds ({cross[0][:12]} vs {cross[1][:12]}); a set or dict order leaks into the output")
    report["cross_process_sha256"] = cross[0]
    write_json("determinism.json", report)
    return f"seed 0 gives identical sha256 in-process and across hash seeds (ownit {cross[0][:12]})"


# ---------------------------------------------------------------- V4
def disk_lines(root: str, relpath: str) -> list[str]:
    with open(os.path.join(root, *relpath.split("/")), "r", encoding="utf-8-sig") as handle:
        lines = handle.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def check_evidence() -> str:
    total = 0
    bad: list[str] = []
    kinds: set[str] = set()
    per_corpus: dict[str, int] = {}
    for name in CORPORA:
        facts, _graph, qs = analyse(name)
        cache: dict[str, list[str]] = {}
        count = 0
        for q in qs:
            kinds.add(q.kind)
            if not q.evidence:
                bad.append(f"{name}:{q.id} has no evidence")
                continue
            ids = {opt["key"]: opt["id"] for opt in q.options}
            path = [ids[k] for k in q.answer] if q.kind == "chain" else []
            for index, ev in enumerate(q.evidence):
                total += 1
                count += 1
                lines = cache.setdefault(ev.relpath, disk_lines(facts.root, ev.relpath))
                ok = 1 <= ev.start <= ev.end <= len(lines) and ev.lines == lines[ev.start - 1 : ev.end] and bool(ev.note)
                if ok and q.kind == "callee":
                    ok = facts.funcs[ids[q.answer[0]]].name in "\n".join(ev.lines)
                if ok and q.kind == "chain":
                    ok = facts.funcs[path[index + 1]].name in "\n".join(ev.lines)
                if not ok:
                    bad.append(f"{name}:{q.id}:{ev.relpath}:{ev.start}-{ev.end}")
        per_corpus[name] = count
    missing = enabled_kinds() - kinds
    write_json("evidence.json", {"snippets": total, "mismatched": len(bad), "per_corpus": per_corpus, "kinds_seen": sorted(kinds)})
    expect(total > 0, "no evidence snippets were generated")
    expect(not bad, f"{len(bad)} of {total} evidence snippets do not match the source: " + "; ".join(bad[:4]))
    expect(not missing, f"no questions of kind {sorted(missing)} were generated, so those kinds are unchecked")
    return f"100% of {total} snippets match the source lines (kinds: {', '.join(sorted(kinds))})"


# ---------------------------------------------------------------- V5
def check_trace() -> str:
    parts = []
    failures = []
    for corpus in ("ownit", "textwrap"):
        out = os.path.join(RESULTS, f"trace_{corpus}.json")
        os.makedirs(RESULTS, exist_ok=True)
        if os.path.exists(out):
            os.remove(out)
        proc = subprocess.run(
            [sys.executable, "-m", "ownit.trace_check", "--corpus", corpus, "--out", out],
            cwd=ROOT, capture_output=True, text=True, timeout=900,
        )
        expect(os.path.exists(out), f"{corpus}: trace_check wrote no result file. stderr: {tail(proc.stderr)}")
        data = read_json(out)
        parts.append(
            f"{corpus}: {data['observed_edges']} runtime edges, {data['violation_count']} violations, "
            f"{data['contradicted_questions']} contradicted of {data['callee_questions'] + data['impact_questions']} questions"
        )
        if proc.returncode != 0:
            failures.append(f"{corpus}: {tail(proc.stderr)}")
    expect(not failures, "; ".join(failures) + " | " + "; ".join(parts))
    return "; ".join(parts)


# ---------------------------------------------------------------- V6
def check_staleness() -> str:
    out = os.path.join(RESULTS, "mutate.json")
    os.makedirs(RESULTS, exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    proc = subprocess.run(
        [sys.executable, "-m", "ownit.mutate_check", "--out", out, "--n", "10", "--seed", "0"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800,
    )
    expect(os.path.exists(out), f"mutate_check wrote no result file. stderr: {tail(proc.stderr)}")
    data = read_json(out)
    detail = (
        f"precision={data['precision']} recall={data['recall']} over {data['n']} body mutations; "
        f"{data['format_only_stale']} stale from formatting-only edits; rename orphaned {data['rename_orphaned_pct']}% (reported)"
    )
    expect(proc.returncode == 0, f"{tail(proc.stderr)} | {detail}")
    expect(data["n"] >= 10, f"only {data['n']} body mutations were possible; the claim needs at least 10 | {detail}")
    return detail


# ---------------------------------------------------------------- V7
def find_def(tree: ast.Module, name: str, end_line: int):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name and node.end_lineno == end_line:
            return node
    return None


def append_body_statement(work: str, info) -> bool:
    path = os.path.join(work, *info.relpath.split("/"))
    with open(path, "r", encoding="utf-8-sig") as handle:
        text = handle.read()
    node = find_def(ast.parse(text), info.name, info.end_line)
    if node is None or node.body[0].lineno == node.lineno:
        return False
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines.insert(info.end_line, " " * node.body[0].col_offset + "_own_mut = 0")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return True


def check_end_to_end() -> str:
    with tempfile.TemporaryDirectory() as scratch:
        work = os.path.join(scratch, "todo_agent")
        shutil.copytree(os.path.join(ROOT, "examples", "todo_agent"), work, ignore=shutil.ignore_patterns("__pycache__"))
        qfile = os.path.join(scratch, "questions.json")
        proc = subprocess.run(
            [sys.executable, "-m", "ownit.cli", "scan", work, "--json", qfile],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
        expect(proc.returncode == 0, f"cli scan of the scratch copy failed: {tail(proc.stderr)}")
        questions = read_json(qfile)["questions"]
        expect(len(questions) >= 15, f"only {len(questions)} questions for the demo repo; expected at least 15")
        facts = scan_path(work)
        graph = build_graph(facts)
        state_path = os.path.join(scratch, "state.json")

        with running_server(work, state_path) as base:
            report = call("GET", base, "/api/report")
            overall = report["ownership"]["overall"]
            expect(overall["total"] == len(questions) and overall["new"] == len(questions), f"fresh report should show {len(questions)} new questions, got {overall}")

            listing = call("GET", base, "/api/questions?status=new&limit=1000")["questions"]
            expect(len(listing) == len(questions), f"/api/questions listed {len(listing)} of {len(questions)}")
            for row in listing:
                expect(set(row) == {"id", "kind", "select", "prompt", "module", "options", "status", "changed"}, f"question row has unexpected fields {sorted(row)}")
                expect(all(set(opt) == {"key", "label"} for opt in row["options"]), "option rows leak internal fields")

            first = next(q for q in questions if q["kind"] == "callee")
            wrong = next(opt["key"] for opt in first["options"] if opt["key"] not in first["answer"])
            res = call("POST", base, "/api/answer", {"id": first["id"], "answer": [wrong]})
            expect(res["correct"] is False and res["answer"] == first["answer"], "a wrong answer was graded as correct")
            missed = call("GET", base, "/api/questions?status=missed&limit=1000")["questions"]
            expect([m["id"] for m in missed] == [first["id"]], "the missed question is not listed as missed")

            call("POST", base, "/api/answer", {"id": first["id"], "answer": ["zz"]}, want=400)
            call("POST", base, "/api/answer", {"id": "000000000000", "answer": ["a"]}, want=404)
            call("POST", base, "/api/answer", {"answer": ["a"]}, want=400)

            for q in questions:
                res = call("POST", base, "/api/answer", {"id": q["id"], "answer": q["answer"]})
                expect(res["correct"] is True, f"correct answer for {q['id']} ({q['kind']}) was graded wrong")
                expect(len(res["explain"]) >= 1 and all(set(e) == {"relpath", "start", "end", "lines", "note"} for e in res["explain"]), f"explain payload malformed for {q['id']}")
            report = call("GET", base, "/api/report")
            overall = report["ownership"]["overall"]
            expect(overall["owned"] == len(questions) and overall["stale"] == 0 and overall["validity_pct"] == 100.0, f"after answering everything the report shows {overall}")
            with open(state_path, "r", encoding="utf-8") as handle:
                expect(len(json.load(handle)["answers"]) == len(questions), "the state file did not record every answer")

            involved_count: dict[str, int] = {}
            for q in questions:
                for fid in q["involved"]:
                    involved_count[fid] = involved_count.get(fid, 0) + 1
            target = None
            for fid in sorted(involved_count, key=lambda f: (-involved_count[f], f)):
                if append_body_statement(work, facts.funcs[fid]):
                    target = fid
                    break
            expect(target is not None, "no function with a multi-line body was available to edit")
            expected_ids = {q["id"] for q in questions if target in q["involved"]}

            rescanned = call("POST", base, "/api/rescan", {})
            expect(rescanned["questions"] == len(questions), f"rescan changed the question count to {rescanned['questions']}")
            expect(rescanned["stale"] == len(expected_ids), f"editing {target} made {rescanned['stale']} questions stale, expected exactly {len(expected_ids)}")
            expect(rescanned["orphans"] == 0, f"{rescanned['orphans']} answers were orphaned by a body-only edit")
            stale = call("GET", base, "/api/questions?status=stale&limit=1000")["questions"]
            expect({row["id"] for row in stale} == expected_ids, "the stale list differs from the questions that depend on the edited function")
            expect(all(row["changed"] == [target] for row in stale), f"stale questions should report only {target} as changed")

            card = call("GET", base, "/api/card?id=" + urllib.parse.quote(target))
            expect(card["callers"] == sorted(graph.callers[target]) and card["callees"] == sorted(graph.callees[target]), "card callers or callees differ from the call graph")
            expect(card["summary"] == f"Used by {len(card['callers'])} functions, calls {len(card['callees'])}", f"card summary is {card['summary']!r}")
            call("GET", base, "/api/card?id=nope.nothing", want=404)

            for bad_path in ("../../README.md", "/etc/passwd"):
                call("GET", base, "/api/source?path=" + urllib.parse.quote(bad_path) + "&start=1&end=3", want=400)
            call("GET", base, "/api/source?path=storage.py&start=1&end=500", want=400)
            source = call("GET", base, "/api/source?path=storage.py&start=1&end=3")
            on_disk = disk_lines(work, "storage.py")[:3]
            expect([ln["text"] for ln in source["lines"]] == on_disk, "/api/source returned lines that differ from the file")

    return f"{len(questions)} questions answered over HTTP; editing {target} made exactly {len(expected_ids)} go stale; card, source guard and error codes verified"


# ---------------------------------------------------------------- V8
def check_ui() -> str:
    with tempfile.TemporaryDirectory() as scratch:
        with running_server(corpus_path("todo_agent"), os.path.join(scratch, "state.json")) as base:
            status, body, headers = http("GET", base + "/")
            html = body.decode("utf-8")
            expect(status == 200 and 'id="app"' in html, 'GET / must return 200 and contain id="app"')
            expect("text/html" in headers.get("content-type", ""), f"GET / content type is {headers.get('content-type')!r}")
            scripts = ""
            for path, wanted in (("/static/style.css", "text/css"), ("/static/dashboard.js", "javascript"), ("/static/quiz.js", "javascript")):
                status, body, headers = http("GET", base + path)
                expect(status == 200, f"{path} returned HTTP {status}")
                expect(wanted in headers.get("content-type", ""), f"{path} content type is {headers.get('content-type')!r}, expected {wanted}")
                expect(len(body) > 500, f"{path} is suspiciously small ({len(body)} bytes)")
                expect(path in html, f"index.html does not reference {path}")
                if path.endswith(".js"):
                    scripts += body.decode("utf-8")
            status, _, _ = http("GET", base + "/static/../ownit/server.py")
            expect(status == 404, f"a path outside the static whitelist returned HTTP {status}, expected 404")
            missing = [kind for kind in ("loading", "empty", "error", "stale", "all-owned") if f'stateBox("{kind}"' not in scripts]
            expect(not missing, f"the UI scripts never render the {missing} state; add a stateBox(\"<state>\", ...) for each")
    return "GET / has id=\"app\"; style.css, dashboard.js and quiz.js served with correct types; all 5 UI states present"


# ---------------------------------------------------------------- V9
def check_demo_repo() -> str:
    facts, graph, qs = analyse("todo_agent")
    counts = count_kinds(qs)
    kinds = enabled_kinds()
    short = [f"{k}={counts[k]}" for k in sorted(kinds) if counts[k] < 5]
    expect(not short, f"examples/todo_agent needs at least 5 questions of each enabled kind; short: {short}")
    modules = {f.module for f in facts.funcs.values()}
    top_level = [f for f in facts.funcs.values() if f.class_name is None]
    classes = sum(len(c) for c in facts.classes.values())
    expect(len(modules) >= 4, f"fixture has {len(modules)} modules with functions, needs 4")
    expect(len(top_level) >= 12, f"fixture has {len(top_level)} module-level functions, needs 12")
    expect(classes >= 1, "fixture needs at least one class")
    expect(not any(f.decorated for f in facts.funcs.values()), "fixture functions must not use decorators")
    expect(not facts.dynamic, f"fixture must not use getattr/eval-style calls: {sorted(facts.dynamic)}")
    expect(any(len(callers_of(graph, fid)) >= 2 for fid in graph.funcs), "fixture needs a function with at least 2 transitive callers")
    chain_found = False
    for src in sorted(graph.funcs):
        for dst in sorted(reachable(graph, src)):
            path = unique_shortest_path(graph, src, dst)
            if path is not None and 3 <= len(path) <= 5:
                chain_found = True
                break
        if chain_found:
            break
    expect(chain_found, "fixture needs a unique call chain of 3 to 5 functions")
    return f"{len(modules)} modules, {len(top_level)} functions, {classes} class; questions " + ", ".join(f"{k}={counts[k]}" for k in sorted(kinds))


# ---------------------------------------------------------------- V10
def run_proof_check(claims: list[dict]) -> dict:
    claim = {"id": "V10", "name": "proof endpoint", "pass": False, "detail": ""}

    def summary(items: list[dict]) -> dict:
        return {"claims": items, "all_pass": all(item["pass"] for item in items)}

    try:
        write_json("summary.json", summary(claims))
        with tempfile.TemporaryDirectory() as scratch:
            with running_server(corpus_path("todo_agent"), os.path.join(scratch, "state.json")) as base:
                served = call("GET", base, "/api/proof")
                on_disk = read_json(SUMMARY)
                expect(served == on_disk, "/api/proof differs from results/summary.json")
                claim["pass"] = True
                claim["detail"] = f"/api/proof equals results/summary.json ({len(on_disk['claims'])} claims before this one, re-checked after the final write)"
                write_json("summary.json", summary(claims + [claim]))
                served = call("GET", base, "/api/proof")
                on_disk = read_json(SUMMARY)
                if served != on_disk:
                    claim["pass"] = False
                    claim["detail"] = "/api/proof differs from results/summary.json after the final write"
    except CheckFailed as exc:
        claim["pass"] = False
        claim["detail"] = str(exc)
    except Exception as exc:
        traceback.print_exc()
        claim["pass"] = False
        claim["detail"] = f"crashed: {type(exc).__name__}: {exc}"
    return claim


# ---------------------------------------------------------------- driver
CHECKS = [
    ("V1", "deps", check_deps),
    ("V2", "scans", check_scans),
    ("V3", "determinism", check_determinism),
    ("V4", "evidence", check_evidence),
    ("V5", "trace", check_trace),
    ("V6", "staleness", check_staleness),
    ("V7", "end-to-end", check_end_to_end),
    ("V8", "ui ships", check_ui),
    ("V9", "demo repo", check_demo_repo),
]


def execute(claim_id: str, name: str, fn) -> dict:
    try:
        detail = fn()
        passed = True
    except CheckFailed as exc:
        detail, passed = str(exc), False
    except Exception as exc:
        traceback.print_exc()
        detail, passed = f"crashed: {type(exc).__name__}: {exc}", False
    return {"id": claim_id, "name": name, "pass": passed, "detail": detail}


def show(claim: dict) -> None:
    print(f"{'PASS' if claim['pass'] else 'FAIL'} {claim['id']} {claim['name']} :: {claim['detail']}", flush=True)


def main() -> int:
    os.makedirs(RESULTS, exist_ok=True)
    claims: list[dict] = []
    for claim_id, name, fn in CHECKS:
        claim = execute(claim_id, name, fn)
        claims.append(claim)
        show(claim)
    last = run_proof_check(claims)
    claims.append(last)
    show(last)
    write_json("summary.json", {"claims": claims, "all_pass": all(c["pass"] for c in claims)})
    failed = [c["id"] for c in claims if not c["pass"]]
    if failed:
        print(f"{len(failed)} of {len(claims)} checks failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"all {len(claims)} checks passed; results are in {os.path.relpath(RESULTS, ROOT)}/", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
