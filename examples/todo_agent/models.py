# @exports: Task(task_id: int, title: str, due: datetime.date, done: bool, tags: list[str]) -> Task{task_id, title, due, done, tags, label}
# @exports: normalize_title(raw: str) -> str   # collapses whitespace; raises ValueError on an empty title
# @exports: parse_due(text: str) -> datetime.date   # YYYY-MM-DD; raises ValueError with a fix hint
# @exports: make_task(task_id: int, title: str, due_text: str, tags: list[str], done: bool = False) -> Task
# @schema: Task.label = "#<id> <title>"
from __future__ import annotations

import datetime


class Task:
    def __init__(self, task_id: int, title: str, due: datetime.date, done: bool, tags: list[str]) -> None:
        self.task_id = task_id
        self.title = title
        self.due = due
        self.done = done
        self.tags = tags
        self.label = self._build_label()

    def _build_label(self) -> str:
        return f"#{self.task_id} {self.title}"


def normalize_title(raw: str) -> str:
    cleaned = " ".join(raw.split())
    if not cleaned:
        raise ValueError("a task needs a non-empty title; pass the title text as the first argument")
    return cleaned


def parse_due(text: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"due date {text!r} is not a valid YYYY-MM-DD date; use a form like 2026-10-31") from exc


def make_task(task_id: int, title: str, due_text: str, tags: list[str], done: bool = False) -> Task:
    return Task(task_id, normalize_title(title), parse_due(due_text), done, list(tags))
