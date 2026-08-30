#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
TEMPLATE="$ROOT_DIR/.zshrc.template"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/dotfactory-dotfiles.XXXXXX")
TEST_HOME="$TEST_ROOT/home"
TEST_BIN="$TEST_ROOT/bin"

cleanup() {
    rm -rf "$TEST_ROOT"
}
trap cleanup EXIT HUP INT TERM

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

assert_contains() {
    output=$1
    expected=$2
    label=$3

    case "$output" in
        *"$expected"*) ;;
        *) fail "$label: missing $expected" ;;
    esac
}

make_fakes() {
    mkdir -p "$TEST_HOME/.local/bin" "$TEST_BIN"

    printf '%s\n' \
        '#!/usr/bin/env sh' \
        'printf "command=claude\n"' \
        'printf "child_key=%s\n" "${ANTHROPIC_API_KEY-__UNSET__}"' \
        'printf "args=%s\n" "$*"' \
        > "$TEST_BIN/claude"

    printf '%s\n' \
        '#!/usr/bin/env sh' \
        'printf "command=omp\n"' \
        'printf "child_key=%s\n" "${ANTHROPIC_API_KEY-__UNSET__}"' \
        'printf "args=%s\n" "$*"' \
        > "$TEST_HOME/.local/bin/omp"

    chmod +x "$TEST_BIN/claude" "$TEST_HOME/.local/bin/omp"
}

check_static_contract() {
    /bin/zsh -n "$TEMPLATE"

    if grep -Eq 'sk-ant-|^[[:space:]]*export[[:space:]]+ANTHROPIC_API_KEY=' "$TEMPLATE"; then
        fail ".zshrc.template contains a key or global ANTHROPIC_API_KEY export"
    fi
}

check_claude_oauth() {
    result=$(
        HOME="$TEST_HOME" \
        PATH="$TEST_BIN:/usr/bin:/bin" \
        ANTHROPIC_API_KEY=PARENT_TEST_KEY \
        /bin/zsh -c '
            source "$1"
            claude-operator resume-marker
            print -r -- "parent_key=${ANTHROPIC_API_KEY-__UNSET__}"
        ' dotfiles-test "$TEMPLATE"
    )

    assert_contains "$result" 'child_key=__UNSET__' 'claude-operator OAuth route'
    assert_contains "$result" 'args=resume-marker' 'claude-operator arguments'
    assert_contains "$result" 'parent_key=PARENT_TEST_KEY' 'claude-operator parent isolation'
}

check_claude_api() {
    result=$(
        HOME="$TEST_HOME" \
        PATH="$TEST_BIN:/usr/bin:/bin" \
        ANTHROPIC_API_KEY=PARENT_TEST_KEY \
        DOTFACTORY_API_ANTHROPIC_API_KEY=API_TEST_KEY \
        /bin/zsh -c '
            source "$1"
            claude-api resume-marker
            print -r -- "parent_key=${ANTHROPIC_API_KEY-__UNSET__}"
        ' dotfiles-test "$TEMPLATE"
    )

    assert_contains "$result" 'child_key=API_TEST_KEY' 'claude-api alternate route'
    assert_contains "$result" 'args=resume-marker' 'claude-api arguments'
    assert_contains "$result" 'parent_key=PARENT_TEST_KEY' 'claude-api parent isolation'

    set +e
    result=$(
        HOME="$TEST_HOME" \
        PATH="$TEST_BIN:/usr/bin:/bin" \
        DOTFACTORY_API_ANTHROPIC_API_KEY= \
        /bin/zsh -c 'source "$1"; claude-api' dotfiles-test "$TEMPLATE" 2>&1
    )
    exit_code=$?
    set -e

    [ "$exit_code" -eq 1 ] || fail "claude-api accepts a missing alternate key"
    assert_contains "$result" 'DOTFACTORY_API_ANTHROPIC_API_KEY is not set' 'claude-api missing key'
}

check_omp_profiles() {
    default=$(
        HOME="$TEST_HOME" \
        PATH="$TEST_BIN:/usr/bin:/bin" \
        ANTHROPIC_API_KEY=PARENT_TEST_KEY \
        DOTFACTORY_DEFAULT_ANTHROPIC_API_KEY=DEFAULT_TEST_KEY \
        /bin/zsh -c '
            source "$1"
            omp-claude resume-marker
            print -r -- "parent_key=${ANTHROPIC_API_KEY-__UNSET__}"
        ' dotfiles-test "$TEMPLATE"
    )

    assert_contains "$default" 'child_key=DEFAULT_TEST_KEY' 'omp-claude default route'
    assert_contains "$default" 'args=--models claude-opus-5,claude-sonnet-5,claude-fable-5 resume-marker' 'omp-claude models'
    assert_contains "$default" 'parent_key=PARENT_TEST_KEY' 'omp-claude parent isolation'

    alternate=$(
        HOME="$TEST_HOME" \
        PATH="$TEST_BIN:/usr/bin:/bin" \
        ANTHROPIC_API_KEY=PARENT_TEST_KEY \
        DOTFACTORY_API_ANTHROPIC_API_KEY=API_TEST_KEY \
        /bin/zsh -c '
            source "$1"
            omp-api resume-marker
            print -r -- "parent_key=${ANTHROPIC_API_KEY-__UNSET__}"
        ' dotfiles-test "$TEMPLATE"
    )

    assert_contains "$alternate" 'child_key=API_TEST_KEY' 'omp-api alternate route'
    assert_contains "$alternate" 'args=--profile=omp-api --models claude-opus-5,claude-sonnet-5,claude-fable-5 resume-marker' 'omp-api profile and models'
    assert_contains "$alternate" 'parent_key=PARENT_TEST_KEY' 'omp-api parent isolation'
}

check_harness_config() {
    /bin/bash "$ROOT_DIR/harness-config/test.sh"
}

main() {
    make_fakes
    check_static_contract
    check_claude_oauth
    check_claude_api
    check_omp_profiles
    check_harness_config
    printf 'PASS\n'
}

if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
    main "$@"
fi
