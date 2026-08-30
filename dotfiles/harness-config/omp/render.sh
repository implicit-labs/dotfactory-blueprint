#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
PROFILE=${1:-}

if [ -z "$PROFILE" ] || [ "$#" -ne 1 ]; then
    printf 'usage: %s <profile>\n' "$0" >&2
    exit 2
fi

APPROVAL_MODE=$(awk -F '\t' -v profile="$PROFILE" '
    $1 == profile { print $2 }
' "$ROOT_DIR/profiles.tsv")

if [ -z "$APPROVAL_MODE" ]; then
    printf 'unknown OMP profile: %s\n' "$PROFILE" >&2
    exit 2
fi

sed "s/__APPROVAL_MODE__/$APPROVAL_MODE/g" "$ROOT_DIR/config.yml.template"
