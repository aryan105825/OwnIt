# OwnIt

Comprehension quizzes on your own AI-written Python repo. Answers come from the static call graph (no LLM), and a question goes stale the moment the code it depends on changes.

AI-written code runs, but its owner often can't explain it, and the next AI edit silently invalidates what they understood. OwnIt asks plain-language structural questions about your code, grades them against exact static-analysis answers, shows the source evidence, and tracks "understanding still valid" per module.

## Requirements

- Python 3.10 or newer
- Nothing else. Standard library only, no packages to install, no API keys.

## Quick start

```
python -m ownit.cli scan examples/todo_agent
python -m ownit.server examples/todo_agent
```

Then open the URL printed after `LISTENING`.

## Configuration

Copy `.env.example` to `.env` if you want to change the defaults. Variables are `OWNIT_HOST`, `OWNIT_PORT` and `OWNIT_STATE`; all are optional.

## Question types

- **Callee:** which function does X call directly? (pick one)
- **Impact:** if you change X, which of these could break? (pick all that apply)
- **Chain:** put the shortest call chain from X to Y in order (optional).
- **Function cards:** "Used by N functions, calls M", computed without an LLM.

Anything uncertain (dynamic calls, unresolved attributes, shared names) is excluded from questions.

## Proof

The Proof tab in the UI and `scripts/verify.sh` check every claim against real measurements and write results to `results/`.

## Project docs

`scope.md`, `prd.md` and `spec.md` describe scope, requirements and design.
