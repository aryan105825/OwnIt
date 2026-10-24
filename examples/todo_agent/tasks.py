# @exports: find_task(tasks: list[Task], task_id: int) -> Task   # raises LookupError with a fix hint
# @exports: open_tasks(tasks: list[Task]) -> list[Task]
# @exports: rank_open_tasks(tasks: list[Task], today: datetime.date) -> list[Task]   # most urgent first, ties by id
# @exports: add_new_task(directory: str, title: str, due_text: str, tags: list[str]) -> Task   # persists
# @exports: complete_task(directory: str, task_id: int) -> Task   # persists
# @exports: daily_digest(directory: str, today: datetime.date) -> str
# @imports: models.py:Task, models.py:make_task(task_id, title, due_text, tags, done)
# @imports: schedule.py:reminder_text(task, today), schedule.py:urgency_score(task, today)
# @imports: storage.py:load_tasks(directory), storage.py:next_task_id(tasks), storage.py:save_tasks(directory, tasks)
from __future__ import annotations

import datetime

from .models import Task, make_task
from .schedule import reminder_text, urgency_score
from .storage import load_tasks, next_task_id, save_tasks


def find_task(tasks: list[Task], task_id: int) -> Task:
    for task in tasks:
        if task.task_id == task_id:
            return task
    raise LookupError(f"no task with id {task_id}; run the digest command to see the current ids")


def open_tasks(tasks: list[Task]) -> list[Task]:
    return [task for task in tasks if not task.done]


def rank_open_tasks(tasks: list[Task], today: datetime.date) -> list[Task]:
    pending = open_tasks(tasks)
    return sorted(pending, key=lambda item: (-urgency_score(item, today), item.task_id))


def add_new_task(directory: str, title: str, due_text: str, tags: list[str]) -> Task:
    existing = load_tasks(directory)
    created = make_task(next_task_id(existing), title, due_text, tags)
    save_tasks(directory, existing + [created])
    return created


def complete_task(directory: str, task_id: int) -> Task:
    existing = load_tasks(directory)
    target = find_task(existing, task_id)
    target.done = True
    save_tasks(directory, existing)
    return target


def daily_digest(directory: str, today: datetime.date) -> str:
    ranked = rank_open_tasks(load_tasks(directory), today)
    if not ranked:
        return "Nothing open. Add a task with: add TITLE --due YYYY-MM-DD"
    return "\n".join(reminder_text(item, today) for item in ranked)
