# @exports: DYNAMIC_NAMES: set[str] = {"getattr","setattr","eval","exec","__import__","globals","locals","vars"}
# @exports: extract_body(fn: ast.FunctionDef|ast.AsyncFunctionDef) -> tuple[list[RawCall], set[str], bool]   # (calls sorted by (line,kind,name,base), bare-name Load refs, uses_dynamic); walks the WHOLE body incl. nested defs and lambdas, not decorators or defaults
# @imports: model.py:RawCall(kind: str, name: str, base: str|None, line: int)
# @schema: RawCall kind "name" = f(); "self" = self.x() or cls.x() (direct Name base only); "attr" = any other attribute call
# @schema: RawCall.base = the Name id when the attribute's value is directly a Name, else None (so a.b.c(), f().x() and s[0].x() have base None; narrower than "leftmost Name" on purpose so graph rule R3 never mis-resolves a chained attribute)
# @schema: calls whose callee is neither a Name nor an Attribute (e.g. f()(), (lambda: 0)()) produce no RawCall
from __future__ import annotations

import ast

from .model import RawCall

DYNAMIC_NAMES = {"getattr", "setattr", "eval", "exec", "__import__", "globals", "locals", "vars"}

_SELF_BASES = ("self", "cls")


def _classify(call: ast.Call) -> RawCall | None:
    func = call.func
    if isinstance(func, ast.Name):
        return RawCall(kind="name", name=func.id, base=None, line=call.lineno)
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Name):
            kind = "self" if value.id in _SELF_BASES else "attr"
            return RawCall(kind=kind, name=func.attr, base=value.id, line=call.lineno)
        return RawCall(kind="attr", name=func.attr, base=None, line=call.lineno)
    return None


def extract_body(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[RawCall], set[str], bool]:
    calls: list[RawCall] = []
    refs: set[str] = set()
    uses_dynamic = False
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                raw = _classify(node)
                if raw is not None:
                    calls.append(raw)
                    if raw.kind == "name" and raw.name in DYNAMIC_NAMES:
                        uses_dynamic = True
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                refs.add(node.id)
    calls.sort(key=lambda c: (c.line, c.kind, c.name, c.base or ""))
    return calls, refs, uses_dynamic
