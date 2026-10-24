# @exports: load_state(path: str) -> dict   # missing file => {"version":1,"answers":{}}; unreadable/invalid file raises ValueError (never silently reset)
# @exports: save_state(state: dict, path: str) -> None   # tmp file + os.replace; creates parent dirs; JSON sort_keys, ensure_ascii=False, indent=1, trailing newline
# @exports: record_answer(state: dict, q: Question, correct: bool) -> None   # fps overwritten with q.fps; attempts += 1
# @exports: classify(state: dict, questions: list[Question]) -> dict[str, {"status": "new"|"owned"|"stale"|"missed", "changed": list[str]}]
# @exports: orphans(state: dict, questions: list[Question]) -> list[str]   # answered ids absent from current questions, sorted
# @exports: ownership(questions: list[Question], statuses: dict) -> {"overall": O, "by_module": {module: O}}
# @imports: model.py:Question
# @schema: state = {"version": 1, "answers": {qid: {"correct": bool, "fps": {func id: str}, "attempts": int, "answered_at": str (UTC ISO-8601, seconds)}}}
# @schema: O = {"total","new","owned","stale","missed": int, "coverage_pct": float, "validity_pct": float|None}
# @schema: coverage_pct = 100*(owned+stale)/total (0.0 when total is 0); validity_pct = 100*owned/(owned+stale) or None when owned+stale is 0; both rounded to 1 decimal (PERCENT 0-100, not a 0-1 fraction)
# @schema: stale "changed" = sorted func ids whose stored fingerprint differs from the question's current fps (a function that appeared or vanished from fps counts as changed)
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .model import Question

_VERSION = 1


def _empty() -> dict:
    return {"version": _VERSION, "answers": {}}


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return _empty()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"state file {path} is not valid JSON ({exc}). Fix or delete the file, or pass --state to use another one."
        ) from exc
    if not isinstance(state, dict) or state.get("version") != _VERSION or not isinstance(state.get("answers"), dict):
        raise ValueError(
            f"state file {path} does not have the expected shape (version {_VERSION} with an 'answers' object). "
            "Delete it to start fresh, or pass --state to use another file."
        )
    return state


def save_state(state: dict, path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, sort_keys=True, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def record_answer(state: dict, q: Question, correct: bool) -> None:
    answers = state.setdefault("answers", {})
    previous = answers.get(q.id)
    attempts = (previous["attempts"] if previous else 0) + 1
    answers[q.id] = {
        "correct": bool(correct),
        "fps": dict(q.fps),
        "attempts": attempts,
        "answered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def classify(state: dict, questions: list[Question]) -> dict[str, dict]:
    answers = state.get("answers", {})
    result: dict[str, dict] = {}
    for q in questions:
        entry = answers.get(q.id)
        if entry is None:
            result[q.id] = {"status": "new", "changed": []}
        elif not entry["correct"]:
            result[q.id] = {"status": "missed", "changed": []}
        else:
            stored = entry["fps"]
            changed = sorted(fid for fid in set(stored) | set(q.fps) if stored.get(fid) != q.fps.get(fid))
            if changed:
                result[q.id] = {"status": "stale", "changed": changed}
            else:
                result[q.id] = {"status": "owned", "changed": []}
    return result


def orphans(state: dict, questions: list[Question]) -> list[str]:
    current = {q.id for q in questions}
    return sorted(qid for qid in state.get("answers", {}) if qid not in current)


def _tally(questions: list[Question], statuses: dict) -> dict:
    counts = {"new": 0, "owned": 0, "stale": 0, "missed": 0}
    for q in questions:
        status = statuses.get(q.id, {"status": "new"})["status"]
        counts[status] += 1
    total = len(questions)
    known = counts["owned"] + counts["stale"]
    coverage = round(100.0 * known / total, 1) if total else 0.0
    validity = round(100.0 * counts["owned"] / known, 1) if known else None
    return {
        "total": total,
        "new": counts["new"],
        "owned": counts["owned"],
        "stale": counts["stale"],
        "missed": counts["missed"],
        "coverage_pct": coverage,
        "validity_pct": validity,
    }


def ownership(questions: list[Question], statuses: dict) -> dict:
    by_module: dict[str, list[Question]] = {}
    for q in questions:
        by_module.setdefault(q.module, []).append(q)
    return {
        "overall": _tally(questions, statuses),
        "by_module": {module: _tally(qs, statuses) for module, qs in sorted(by_module.items())},
    }
