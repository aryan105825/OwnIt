# @exports: days_until(due: datetime.date, today: datetime.date) -> int   # negative when the date has passed
# @exports: is_overdue(task: Task, today: datetime.date) -> bool
# @exports: is_due_soon(task: Task, today: datetime.date, window_days: int = 2) -> bool
# @exports: urgency_score(task: Task, today: datetime.date) -> int   # 100 overdue, 50 due soon, 0 otherwise
# @exports: reminder_text(task: Task, today: datetime.date) -> str
# @imports: models.py:Task
from __future__ import annotations

import datetime

from .models import Task


def days_until(due: datetime.date, today: datetime.date) -> int:
    return (due - today).days


def is_overdue(task: Task, today: datetime.date) -> bool:
    return not task.done and days_until(task.due, today) < 0


def is_due_soon(task: Task, today: datetime.date, window_days: int = 2) -> bool:
    return not task.done and 0 <= days_until(task.due, today) <= window_days


def urgency_score(task: Task, today: datetime.date) -> int:
    if is_overdue(task, today):
        return 100
    if is_due_soon(task, today):
        return 50
    return 0


def reminder_text(task: Task, today: datetime.date) -> str:
    score = urgency_score(task, today)
    days = days_until(task.due, today)
    if score == 100:
        when = f"overdue by {-days} day(s)"
    elif days == 0:
        when = "due today"
    else:
        when = f"due in {days} day(s)"
    return f"[{score:>3}] {task.label} - {when}"
