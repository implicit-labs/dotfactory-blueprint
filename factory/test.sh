#!/usr/bin/env bash
set -euo pipefail

factory_dir="$(cd "$(dirname "$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1

/bin/bash -n "$factory_dir/test.sh"
python3 "$factory_dir/lint.py"
python3 "$factory_dir/render_workflow.py" --check
PYTHONPATH="$factory_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m unittest discover -s "$factory_dir/tests" -p 'test_*.py'
