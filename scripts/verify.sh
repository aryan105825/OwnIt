#!/usr/bin/env bash
# @exports: one command, `bash scripts/verify.sh`; runs scripts/verify.py (checks V1..V10) from the repo root and exits with its status
# @imports: scripts/verify.py:main() -> int
# @schema: prints "PASS|FAIL Vn <name> :: <detail>" per claim; writes results/summary.json plus the measured results/*.json files; exit 0 only if every check passes
# @schema: needs python3 (or python) 3.10+ on PATH and nothing else; reads no environment variables
set -u

cd "$(dirname "$0")/.." || { echo "FAIL setup: cannot enter the repository root. Run this script from inside the repository." >&2; exit 1; }

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "FAIL setup: python3 was not found on PATH. Install Python 3.10 or newer and retry." >&2
  exit 1
fi

mkdir -p results
"$PY" scripts/verify.py
exit $?
