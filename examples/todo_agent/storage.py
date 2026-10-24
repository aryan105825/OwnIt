# @exports: storage_path(directory: str) -> str   # <directory>/tasks.json
# @exports: load_tasks(directory: str) -> list[Task]   # [] when the file does not exist
# @exports: save_tasks(directory: str, tasks: list[Task]) -> None   # creates the directory
# @exports: task_to_row(task: Task) -> dict   # {"id","title","due" (ISO date),"tags","done"}
# @exports: next_task_id(tasks: list[Task]) -> int   # max id + 1, or 1 for an empty list
# @imports: models.py:Task, models.py:make_task(task_id, title, due_text, tags, done)
# @schema: tasks.json = list of task_to_row dicts, sorted keys, indent 1
from __future__ import annotations

import json
import os

from .models import Task, make_task


def storage_path(directory: str) -> str:
    return os.path.join(directory, "tasks.json")


def task_to_row(task: Task) -> dict:
    return {
        "id": task.task_id,
        "title": task.title,
        "due": task.due.isoformat(),
        "tags": list(task.tags),
        "done": task.done,
    }


def next_task_id(tasks: list[Task]) -> int:
    return max((task.task_id for task in tasks), default=0) + 1


def load_tasks(directory: str) -> list[Task]:
    path = storage_path(directory)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        rows = json.load(handle)
    return [make_task(row["id"], row["title"], row["due"], row["tags"], row["done"]) for row in rows]


def save_tasks(directory: str, tasks: list[Task]) -> None:
    path = storage_path(directory)
    rows = [task_to_row(task) for task in tasks]
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, sort_keys=True, indent=1)
        handle.write("\n")
