#!/usr/bin/env bash
# Start the synthetic SUT (seeded defects on by default) and the QA agent workspace, loopback only.
export QA_MODEL_PROVIDER=replay
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
export SUT_DEFECTS="${SUT_DEFECTS-all}"
"$PY" -m uvicorn sut.app:app --host 127.0.0.1 --port 8601 &
SUT_PID=$!
trap 'kill $SUT_PID 2>/dev/null' EXIT
echo "SUT on http://127.0.0.1:8601 (defects: ${SUT_DEFECTS:-none})"
echo "Workspace on http://127.0.0.1:8600"
"$PY" -m qa_agent.server
