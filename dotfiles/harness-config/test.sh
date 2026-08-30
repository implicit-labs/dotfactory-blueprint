#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dotfactory-harness-config.XXXXXX")

cleanup() {
    rm -rf "$TEST_ROOT"
}
trap cleanup EXIT HUP INT TERM

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

assert_line() {
    expected=$1
    file=$2
    label=$3

    grep -Fqx "$expected" "$file" || fail "$label: missing $expected"
}

check_no_secrets() {
    if grep -REq "sk-ant-[A-Za-z0-9]|(api[_-]?key|token|secret)[[:space:]]*[:=][[:space:]]*[\"']?[A-Za-z0-9_-]{16}" "$ROOT_DIR"; then
        fail "harness config contains a credential-like value"
    fi
}

check_omp() {
    /bin/bash -n "$ROOT_DIR/omp/render.sh"

    "$ROOT_DIR/omp/render.sh" default > "$TEST_ROOT/omp-default.yml"
    "$ROOT_DIR/omp/render.sh" omp-api > "$TEST_ROOT/omp-api.yml"

    assert_line '  approvalMode: write' "$TEST_ROOT/omp-default.yml" 'default OMP approval'
    assert_line '  approvalMode: yolo' "$TEST_ROOT/omp-api.yml" 'omp-api approval'

    for file in "$TEST_ROOT/omp-default.yml" "$TEST_ROOT/omp-api.yml"; do
        assert_line 'web_search:' "$file" 'OMP web search'
        assert_line 'fetch:' "$file" 'OMP web fetch'
        assert_line '  fetch: auto' "$file" 'OMP automatic fetch provider'
        assert_line 'computer:' "$file" 'OMP computer use'
        assert_line 'async:' "$file" 'OMP async work'
        if grep -q '__APPROVAL_MODE__' "$file"; then
            fail "OMP render left an unresolved approval placeholder"
        fi
    done

    for server in linear Neon vercel:vercel supabase:supabase node_repl openaiDeveloperDocs pencil render; do
        awk -F '\t' -v server="$server" '$1 == server { found = 1 } END { exit !found }' \
            "$ROOT_DIR/omp/mcp-required.tsv" || fail "OMP MCP manifest missing $server"
    done
}

check_claude() {
    python3 - "$ROOT_DIR/claude-code/settings.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)

assert config["model"] == "opus[1m]"
assert config["effortLevel"] == "high"
assert config["permissions"]["defaultMode"] == "auto"
assert {"Read", "Edit", "Write", "WebFetch", "WebSearch"} <= set(config["permissions"]["allow"])
assert "ANTHROPIC_API_KEY" not in config.get("env", {})
PY
}

check_codex() {
    config="$ROOT_DIR/codex/config.toml"
    assert_line 'model = "gpt-5.6-sol"' "$config" 'Codex model'
    assert_line 'model_reasoning_effort = "high"' "$config" 'Codex reasoning'
    assert_line 'sandbox_mode = "workspace-write"' "$config" 'Codex sandbox'
    assert_line 'approval_policy = "on-request"' "$config" 'Codex approval'
    assert_line 'goals = true' "$config" 'Codex goals'
    assert_line 'memories = true' "$config" 'Codex memories'
}

main() {
    check_no_secrets
    check_omp
    check_claude
    check_codex
    printf 'PASS harness-config\n'
}

if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
    main "$@"
fi
