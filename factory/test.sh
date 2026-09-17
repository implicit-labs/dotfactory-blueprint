#!/usr/bin/env bash
set -euo pipefail

factory_dir="$(cd "$(dirname "$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
test_python="${DOTFACTORY_TEST_PYTHON:-python3}"

if [ -n "${DOTFACTORY_TEST_PYTHON:-}" ]; then
  case "$test_python" in
    /*) ;;
    *)
      echo "DOTFACTORY_TEST_PYTHON must be an absolute path" >&2
      exit 1
      ;;
  esac
fi

/bin/bash -n "$factory_dir/test.sh"
"$test_python" "$factory_dir/lint.py"
"$test_python" "$factory_dir/render_workflow.py" --check
PYTHONPATH="$factory_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$test_python" -m unittest discover -s "$factory_dir/tests" -p 'test_*.py'
