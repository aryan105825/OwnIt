# @exports: SKIP_DIRS: set[str]
# @exports: scan_path(path: str) -> Facts   # dir or single .py; raises FileNotFoundError if path is missing, ValueError if a file is not .py
# @exports: module_name(relpath: str) -> str   # "a/b/c.py" -> "a.b.c"; "a/__init__.py" -> "a"; root "__init__.py" -> "__init__"
# @exports: fingerprint(fn: ast.FunctionDef|ast.AsyncFunctionDef) -> str   # 16 hex; sha256(ast.dump(copy minus docstring, include_attributes=False))[:16]
# @imports: calls.py:extract_body(fn: ast.FunctionDef|ast.AsyncFunctionDef) -> tuple[list[RawCall], set[str], bool]
# @imports: model.py:Facts(root: str, ...)
# @imports: model.py:FuncInfo(id, module, relpath, name, class_name, first_line, end_line, fingerprint, decorated)
# @schema: Facts.root = absolute directory scanned (for a single file: its directory); relpaths use "/" separators
# @schema: Facts.sources[relpath] = file lines (universal newlines, BOM stripped, no trailing empty element) so line N of the list is ast line N
# @schema: FuncInfo only for module-level defs and methods of module-level classes; nested defs fold into their parent; a duplicate id (e.g. property setter) keeps the LAST definition
# @schema: Facts.imports covers module-level import statements only (not inside defs/classes); relative imports are resolved to absolute dotted names; "from x import *" is ignored
# @schema: Facts.skipped = relpaths that raised SyntaxError/ValueError/UnicodeDecodeError (reported in stats, never silent)
from __future__ import annotations

import ast
import copy
import hashlib
import os

from .calls import extract_body
from .model import Facts, FuncInfo

SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules", ".ownit", "build", "dist"}

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


def module_name(relpath: str) -> str:
    path = relpath.replace("\\", "/")
    if path.endswith(".py"):
        path = path[:-3]
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else "__init__"


def fingerprint(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    clone = copy.deepcopy(fn)
    body = clone.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        clone.body = body[1:]
    dumped = ast.dump(clone, include_attributes=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:16]


def _split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _collect_imports(tree: ast.Module, module: str, relpath: str) -> dict[str, str]:
    is_init = os.path.basename(relpath) == "__init__.py"
    parts = [] if (is_init and module == "__init__") else module.split(".")
    pkg = parts if is_init else parts[:-1]

    out: dict[str, str] = {}
    stack: list[ast.AST] = list(reversed(tree.body))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
                else:
                    first = alias.name.split(".")[0]
                    out[first] = first
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module.split(".") if node.module else []
            else:
                drop = node.level - 1
                if drop > len(pkg):
                    continue  # relative import climbs above the scan root; leave unbound (stays unresolved)
                base = pkg[: len(pkg) - drop]
                if node.module:
                    base = base + node.module.split(".")
            for alias in node.names:
                if alias.name == "*":
                    continue
                out[alias.asname or alias.name] = ".".join(base + [alias.name])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        else:
            stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return out


def _list_relpaths(root: str) -> list[str]:
    relpaths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                relpaths.append(rel.replace(os.sep, "/"))
    relpaths.sort()
    return relpaths


def scan_path(path: str) -> Facts:
    abs_path = os.path.abspath(path)
    if os.path.isdir(abs_path):
        root = abs_path
        relpaths = _list_relpaths(root)
    elif os.path.isfile(abs_path):
        if not abs_path.endswith(".py"):
            raise ValueError(f"{path!r} is not a .py file. Pass a Python file or a directory containing Python files.")
        root = os.path.dirname(abs_path)
        relpaths = [os.path.basename(abs_path)]
    else:
        raise FileNotFoundError(f"{path!r} does not exist. Pass an existing directory or .py file to scan.")

    facts = Facts(root=root)

    for relpath in relpaths:
        full = os.path.join(root, *relpath.split("/"))
        try:
            with open(full, "r", encoding="utf-8-sig") as fh:
                text = fh.read()
        except UnicodeDecodeError:
            facts.skipped.append(relpath)
            continue
        try:
            tree = ast.parse(text, filename=relpath)
        except (SyntaxError, ValueError):
            facts.skipped.append(relpath)
            continue

        module = module_name(relpath)
        facts.sources[relpath] = _split_lines(text)
        facts.imports.setdefault(module, {}).update(_collect_imports(tree, module, relpath))
        class_names = facts.classes.setdefault(module, set())

        def add(fn: ast.FunctionDef | ast.AsyncFunctionDef, class_name: str | None) -> None:
            qualname = f"{class_name}.{fn.name}" if class_name else fn.name
            fid = f"{module}.{qualname}"
            calls, refs, uses_dynamic = extract_body(fn)
            first_line = min([d.lineno for d in fn.decorator_list] + [fn.lineno])
            facts.funcs[fid] = FuncInfo(
                id=fid,
                module=module,
                relpath=relpath,
                name=fn.name,
                class_name=class_name,
                first_line=first_line,
                end_line=fn.end_lineno or fn.lineno,
                fingerprint=fingerprint(fn),
                decorated=bool(fn.decorator_list),
            )
            facts.calls[fid] = calls
            facts.refs[fid] = refs
            if uses_dynamic:
                facts.dynamic.add(fid)
            else:
                facts.dynamic.discard(fid)

        for node in tree.body:
            if isinstance(node, _FUNC_TYPES):
                add(node, None)
            elif isinstance(node, ast.ClassDef):
                class_names.add(node.name)
                for sub in node.body:
                    if isinstance(sub, _FUNC_TYPES):
                        add(sub, node.name)

    return facts
