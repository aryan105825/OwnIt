# @exports: build_parser() -> argparse.ArgumentParser   # --directory, subcommands add TITLE --due D [--tag T], done TASK_ID, digest [--today D]
# @exports: cmd_add(args: argparse.Namespace) -> int
# @exports: cmd_done(args: argparse.Namespace) -> int
# @exports: cmd_digest(args: argparse.Namespace) -> int
# @exports: main(argv: list[str] | None = None) -> int
# @imports: models.py:parse_due(text)
# @imports: tasks.py:add_new_task(directory, title, due_text, tags), tasks.py:complete_task(directory, task_id), tasks.py:daily_digest(directory, today)
from __future__ import annotations

import argparse
import datetime

from .models import parse_due
from .tasks import add_new_task, complete_task, daily_digest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="todo_agent", description="A tiny task list with reminders.")
    parser.add_argument("--directory", default=".todo", help="folder that holds tasks.json")
    sub = parser.add_subparsers(dest="command", required=True)

    add_cmd = sub.add_parser("add", help="add a task")
    add_cmd.add_argument("title", help="what needs doing")
    add_cmd.add_argument("--due", required=True, help="due date as YYYY-MM-DD")
    add_cmd.add_argument("--tag", action="append", default=[], help="label; repeat for several")

    done_cmd = sub.add_parser("done", help="mark a task finished")
    done_cmd.add_argument("task_id", type=int, help="id shown in the digest")

    digest_cmd = sub.add_parser("digest", help="show open tasks, most urgent first")
    digest_cmd.add_argument("--today", default=None, help="pretend today is this YYYY-MM-DD date")
    return parser


def cmd_add(args: argparse.Namespace) -> int:
    created = add_new_task(args.directory, args.title, args.due, args.tag)
    print(f"Added {created.label} (due {created.due.isoformat()})")
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    finished = complete_task(args.directory, args.task_id)
    print(f"Finished {finished.label}")
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    day = parse_due(args.today) if args.today else datetime.date.today()
    print(daily_digest(args.directory, day))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "add":
        return cmd_add(args)
    if args.command == "done":
        return cmd_done(args)
    return cmd_digest(args)


if __name__ == "__main__":
    raise SystemExit(main())
