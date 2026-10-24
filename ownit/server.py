# @exports: make_server(target: str, state_path: str, host: str = "127.0.0.1", port: int = 8765, seed: int = 0) -> ThreadingHTTPServer   # scans target once at start; raises FileNotFoundError/ValueError/OSError loudly
# @exports: main(argv: list[str]|None = None) -> int   # python -m ownit.server PATH [--host H] [--port P (0=ephemeral)] [--state FILE] [--seed N]; prints "LISTENING http://H:P" (flushed); exit 2 on bad input
# @imports: config.py:load_config(environ=None) -> Config; config.py:ConfigError
# @imports: scan.py:scan_path(path: str) -> Facts
# @imports: graph.py:build_graph(facts) -> Graph
# @imports: questions.py:generate(g, facts, seed), questions.py:public_view(q), questions.py:grade(q, answer)
# @imports: state.py:load_state(path), state.py:save_state(state, path), state.py:record_answer(state, q, correct), state.py:classify(state, questions), state.py:orphans(state, questions), state.py:ownership(questions, statuses)
# @imports: model.py:to_json(obj)
# @env: OWNIT_HOST (via config.py)
# @env: OWNIT_PORT (via config.py)
# @env: OWNIT_STATE (via config.py)
# @schema: GET / -> web/index.html; GET /static/{style.css,dashboard.js,quiz.js}
# @schema: GET /api/report -> {"target","stats":{...graph stats...,"questions":int},"ownership":O-tree,"orphans":int}
# @schema: GET /api/questions?status=new|stale|missed|owned|all&module=&kind=&limit=20 -> {"questions":[{id,kind,select,prompt,module,options:[{key,label}],status,changed:[func id]}]}  (default status=all; ordered stale, missed, new, owned then id; limit 1..1000; 400 on bad filters)
# @schema: POST /api/answer {"id":str,"answer":[str]} -> {"correct":bool,"answer":[str],"explain":[{relpath,start,end,lines,note}],"ownership":O-tree}  (400 malformed or unknown option keys or duplicates, 404 unknown id)
# @schema: POST /api/rescan {} -> {"questions":int,"stale":int,"orphans":int,"ownership":O-tree}
# @schema: GET /api/source?path=&start=&end= -> {"relpath","lines":[{"n":int,"text":str}]}  (400 outside root, bad numbers or more than 200 lines; 404 missing file)
# @schema: GET /api/card?id=<func id> -> {"id","relpath","line","callers":[id],"callees":[id],"uncertain_count":int,"summary":"Used by N functions, calls M"}  (N and M count DIRECT resolved callers and callees; 404 unknown id)
# @schema: GET /api/functions -> {"functions":[{id,relpath,line,end_line,module}]}   # ADDITIVE endpoint beyond CONTRACTS: lets the UI list function ids for cards
# @schema: GET /api/proof -> parsed <repo>/results/summary.json, or {"claims":[],"all_pass":false} if the file is missing
# @schema: every error response is JSON {"error": "<what failed and how to fix it>"}; unexpected exceptions print a traceback to stderr and return 500
# @schema: default state file = <scanned root>/.ownit/state.json; state is held in memory under a lock and saved atomically after each answer
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import ConfigError, load_config
from .graph import build_graph
from .model import to_json
from .questions import generate, grade, public_view
from .scan import scan_path
from .state import classify, load_state, orphans, ownership, record_answer, save_state

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(REPO_ROOT, "web")
PROOF_PATH = os.path.join(REPO_ROOT, "results", "summary.json")

STATIC = {
    "style.css": "text/css; charset=utf-8",
    "dashboard.js": "application/javascript; charset=utf-8",
    "quiz.js": "application/javascript; charset=utf-8",
}
STATUSES = ("new", "stale", "missed", "owned")
KINDS = ("callee", "impact", "chain")
STATUS_ORDER = {"stale": 0, "missed": 1, "new": 2, "owned": 3}
MAX_SOURCE_LINES = 200
MAX_BODY_BYTES = 1_000_000
MAX_LIMIT = 1000


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class App:
    """Holds the scanned project and the saved quiz state. Every public method takes the lock."""

    def __init__(self, target: str, state_path: str, seed: int) -> None:
        self.target = os.path.abspath(target)
        self.state_path = state_path
        self.seed = seed
        self.lock = threading.Lock()
        self.state = load_state(state_path)
        self._scan()

    def _scan(self) -> None:
        facts = scan_path(self.target)
        graph = build_graph(facts)
        questions = generate(graph, facts, seed=self.seed)
        self.facts = facts
        self.graph = graph
        self.questions = questions
        self.by_id = {q.id: q for q in questions}

    def _statuses(self) -> dict:
        return classify(self.state, self.questions)

    def report(self) -> dict:
        with self.lock:
            statuses = self._statuses()
            stats = dict(self.graph.stats)
            stats["questions"] = len(self.questions)
            return {
                "target": self.target,
                "stats": stats,
                "ownership": ownership(self.questions, statuses),
                "orphans": len(orphans(self.state, self.questions)),
            }

    def list_questions(self, status: str, module: str, kind: str, limit: int) -> dict:
        with self.lock:
            statuses = self._statuses()
            rows = []
            for q in self.questions:
                info = statuses[q.id]
                if status != "all" and info["status"] != status:
                    continue
                if module and q.module != module:
                    continue
                if kind and q.kind != kind:
                    continue
                row = public_view(q)
                row["status"] = info["status"]
                row["changed"] = info["changed"]
                rows.append(row)
            rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], r["id"]))
            return {"questions": rows[:limit]}

    def answer(self, qid: str, keys: list[str]) -> dict:
        with self.lock:
            q = self.by_id.get(qid)
            if q is None:
                raise HttpError(404, f"unknown question id {qid!r}; the code may have changed, so rescan and reload the quiz")
            valid = {opt["key"] for opt in q.options}
            if not keys or len(set(keys)) != len(keys) or any(k not in valid for k in keys):
                raise HttpError(400, f"answer must be a non-empty list of distinct option keys from {sorted(valid)}")
            correct = grade(q, keys)
            record_answer(self.state, q, correct)
            save_state(self.state, self.state_path)
            return {
                "correct": correct,
                "answer": list(q.answer),
                "explain": [to_json(ev) for ev in q.evidence],
                "ownership": ownership(self.questions, self._statuses()),
            }

    def rescan(self) -> dict:
        with self.lock:
            self._scan()
            statuses = self._statuses()
            return {
                "questions": len(self.questions),
                "stale": sum(1 for info in statuses.values() if info["status"] == "stale"),
                "orphans": len(orphans(self.state, self.questions)),
                "ownership": ownership(self.questions, statuses),
            }

    def card(self, fid: str) -> dict:
        with self.lock:
            func = self.graph.funcs.get(fid)
            if func is None:
                raise HttpError(404, f"unknown function id {fid!r}; pick an id from /api/functions")
            callers = sorted(self.graph.callers[fid])
            callees = sorted(self.graph.callees[fid])
            return {
                "id": fid,
                "relpath": func.relpath,
                "line": func.first_line,
                "callers": callers,
                "callees": callees,
                "uncertain_count": len(self.graph.uncertain[fid]),
                "summary": f"Used by {len(callers)} functions, calls {len(callees)}",
            }

    def functions(self) -> dict:
        with self.lock:
            rows = [
                {"id": f.id, "relpath": f.relpath, "line": f.first_line, "end_line": f.end_line, "module": f.module}
                for f in self.facts.funcs.values()
            ]
            rows.sort(key=lambda r: r["id"])
            return {"functions": rows}

    def source(self, path: str, start: int, end: int) -> dict:
        root = os.path.realpath(self.facts.root)
        if not path:
            raise HttpError(400, "missing 'path'; pass a file path relative to the scanned folder")
        full = os.path.realpath(os.path.join(root, path))
        try:
            inside = os.path.commonpath([full, root]) == root
        except ValueError:
            inside = False
        if not inside:
            raise HttpError(400, f"path {path!r} is outside the scanned folder; use a path relative to it")
        if start < 1 or end < start:
            raise HttpError(400, "start and end must satisfy 1 <= start <= end")
        if end - start + 1 > MAX_SOURCE_LINES:
            raise HttpError(400, f"at most {MAX_SOURCE_LINES} lines can be requested at once; narrow start and end")
        if not os.path.isfile(full):
            raise HttpError(404, f"no file {path!r} in the scanned folder")
        try:
            with open(full, "r", encoding="utf-8-sig") as handle:
                lines = handle.read().split("\n")
        except UnicodeDecodeError:
            raise HttpError(400, f"{path!r} is not UTF-8 text, so it cannot be shown") from None
        if lines and lines[-1] == "":
            lines.pop()
        if start > len(lines):
            raise HttpError(400, f"start {start} is past the end of the file ({len(lines)} lines)")
        stop = min(end, len(lines))
        return {
            "relpath": os.path.relpath(full, root).replace(os.sep, "/"),
            "lines": [{"n": n, "text": lines[n - 1]} for n in range(start, stop + 1)],
        }

    def proof(self) -> dict:
        if not os.path.exists(PROOF_PATH):
            return {"claims": [], "all_pass": False}
        with open(PROOF_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)


def _first(query: dict, name: str, default: str = "") -> str:
    values = query.get(name)
    return values[0] if values else default


def _int(query: dict, name: str, default: int | None = None) -> int:
    raw = _first(query, name, "" if default is None else str(default))
    try:
        return int(raw)
    except ValueError:
        raise HttpError(400, f"query parameter {name!r} must be an integer, got {raw!r}") from None


class Handler(BaseHTTPRequestHandler):
    server_version = "OwnIt"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - signature fixed by the base class
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            self._route(method, parsed.path, parse_qs(parsed.query, keep_blank_values=True))
        except HttpError as exc:
            self._json(exc.status, {"error": exc.message})
        except Exception as exc:
            traceback.print_exc()
            self._json(
                500,
                {"error": f"internal error: {type(exc).__name__}: {exc}. See the server console for the traceback."},
            )

    def _need(self, method: str, allowed: str) -> None:
        if method != allowed:
            raise HttpError(405, f"{self.path.split('?')[0]} only accepts {allowed}")

    def _body(self, required: bool) -> dict:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError:
            raise HttpError(400, f"bad Content-Length {raw_length!r}") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise HttpError(400, f"request body must be between 0 and {MAX_BODY_BYTES} bytes")
        if length == 0:
            if required:
                raise HttpError(400, "this endpoint needs a JSON object body")
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, f"request body is not valid JSON ({exc})") from None
        if not isinstance(body, dict):
            raise HttpError(400, "request body must be a JSON object")
        return body

    def _route(self, method: str, route: str, query: dict) -> None:
        app: App = self.server.app  # type: ignore[attr-defined]

        if route in ("/", "/index.html"):
            self._need(method, "GET")
            self._file("index.html", "text/html; charset=utf-8")
            return
        if route.startswith("/static/"):
            name = route[len("/static/") :]
            if name not in STATIC:
                raise HttpError(404, f"no static file {name!r}")
            self._need(method, "GET")
            self._file(name, STATIC[name])
            return

        if route == "/api/report":
            self._need(method, "GET")
            self._json(200, app.report())
        elif route == "/api/questions":
            self._need(method, "GET")
            status = _first(query, "status", "all")
            if status != "all" and status not in STATUSES:
                raise HttpError(400, f"status must be one of {list(STATUSES) + ['all']}, got {status!r}")
            kind = _first(query, "kind", "")
            if kind and kind not in KINDS:
                raise HttpError(400, f"kind must be one of {list(KINDS)}, got {kind!r}")
            limit = _int(query, "limit", 20)
            if not 1 <= limit <= MAX_LIMIT:
                raise HttpError(400, f"limit must be between 1 and {MAX_LIMIT}")
            self._json(200, app.list_questions(status, _first(query, "module", ""), kind, limit))
        elif route == "/api/answer":
            self._need(method, "POST")
            body = self._body(required=True)
            qid = body.get("id")
            keys = body.get("answer")
            if not isinstance(qid, str) or not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                raise HttpError(400, 'body must look like {"id": "<question id>", "answer": ["a", "b"]}')
            self._json(200, app.answer(qid, keys))
        elif route == "/api/rescan":
            self._need(method, "POST")
            self._body(required=False)
            self._json(200, app.rescan())
        elif route == "/api/source":
            self._need(method, "GET")
            self._json(200, app.source(_first(query, "path"), _int(query, "start"), _int(query, "end")))
        elif route == "/api/card":
            self._need(method, "GET")
            self._json(200, app.card(_first(query, "id")))
        elif route == "/api/functions":
            self._need(method, "GET")
            self._json(200, app.functions())
        elif route == "/api/proof":
            self._need(method, "GET")
            self._json(200, app.proof())
        else:
            raise HttpError(404, f"no such route {route!r}")

    def _file(self, name: str, content_type: str) -> None:
        path = os.path.join(WEB_DIR, name)
        if not os.path.isfile(path):
            raise HttpError(500, f"web file {name} is missing from {WEB_DIR}; restore the web/ folder from the repository")
        with open(path, "rb") as handle:
            data = handle.read()
        self._send(200, content_type, data)

    def _json(self, status: int, obj: object) -> None:
        text = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1) + "\n"
        self._send(status, "application/json; charset=utf-8", text.encode("utf-8"))

    def _send(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def make_server(target: str, state_path: str, host: str = "127.0.0.1", port: int = 8765, seed: int = 0) -> ThreadingHTTPServer:
    app = App(target, state_path, seed)
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ownit.server", description="Serve the OwnIt quiz for a Python project.")
    parser.add_argument("path", help="directory or .py file to quiz you on")
    parser.add_argument("--host", default=None, help="interface to bind (default OWNIT_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port (default OWNIT_PORT or 8765; 0 picks a free port)")
    parser.add_argument("--state", default=None, help="progress file (default OWNIT_STATE or <root>/.ownit/state.json)")
    parser.add_argument("--seed", type=int, default=0, help="seed for option shuffling (default 0)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    host = args.host if args.host is not None else config.host
    port = args.port if args.port is not None else config.port
    if not 0 <= port <= 65535:
        print(f"error: port {port} is out of range. Use a number from 0 to 65535.", file=sys.stderr)
        return 2

    target = os.path.abspath(args.path)
    root = target if os.path.isdir(target) else os.path.dirname(target)
    state_path = args.state or config.state or os.path.join(root, ".ownit", "state.json")

    try:
        server = make_server(target, state_path, host=host, port=port, seed=args.seed)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot start the server on {host}:{port}: {exc}. Choose another port with --port.", file=sys.stderr)
        return 2

    bound_port = server.server_address[1]
    print(f"LISTENING http://{host}:{bound_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
