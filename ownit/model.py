# @exports: FuncInfo(id: str, module: str, relpath: str, name: str, class_name: str|None, first_line: int, end_line: int, fingerprint: str, decorated: bool) [frozen dataclass]
# @exports: RawCall(kind: str, name: str, base: str|None, line: int) [frozen dataclass]
# @exports: Facts(root: str, funcs: dict, calls: dict, refs: dict, dynamic: set, imports: dict, classes: dict, sources: dict, skipped: list) [dataclass, all but root default empty]
# @exports: Evidence(relpath: str, start: int, end: int, lines: list[str], note: str) [dataclass]
# @exports: Question(id: str, kind: str, prompt: str, module: str, options: list[dict], answer: list[str], evidence: list[Evidence], involved: list[str], fps: dict[str, str]) [dataclass]
# @exports: to_json(obj: Any) -> dict|list|scalar   # dataclasses -> dict, sets/frozensets -> sorted lists, tuples -> lists
# @schema: RawCall.kind in {"name","attr","self"}; RawCall.base = leftmost Name id
# @schema: Question.kind in {"callee","impact","chain"}; Question.options = [{"key": str, "label": str}]
# @schema: Question.involved = every function whose change could alter the answer's truth
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FuncInfo:
    id: str
    module: str
    relpath: str
    name: str
    class_name: str | None
    first_line: int
    end_line: int
    fingerprint: str
    decorated: bool


@dataclass(frozen=True)
class RawCall:
    kind: str
    name: str
    base: str | None
    line: int


@dataclass
class Facts:
    root: str
    funcs: dict[str, FuncInfo] = field(default_factory=dict)
    calls: dict[str, list[RawCall]] = field(default_factory=dict)
    refs: dict[str, set[str]] = field(default_factory=dict)
    dynamic: set[str] = field(default_factory=set)
    imports: dict[str, dict[str, str]] = field(default_factory=dict)
    classes: dict[str, set[str]] = field(default_factory=dict)
    sources: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


@dataclass
class Evidence:
    relpath: str
    start: int
    end: int
    lines: list[str]
    note: str


@dataclass
class Question:
    id: str
    kind: str
    prompt: str
    module: str
    options: list[dict[str, str]]
    answer: list[str]
    evidence: list[Evidence]
    involved: list[str]
    fps: dict[str, str]


def to_json(obj: Any) -> Any:
    """Convert dataclasses, sets and tuples into JSON-ready values (sets become sorted lists)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): to_json(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return [to_json(v) for v in sorted(obj)]
    if isinstance(obj, (list, tuple)):
        return [to_json(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    raise TypeError(
        f"to_json cannot serialise {type(obj).__name__}: convert it to a dataclass, dict, list, set or scalar first"
    )
