#!/usr/bin/env bash
set -euo pipefail

bin_dir="${DOTFACTORY_BIN_DIR:-$HOME/.local/bin}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v npm >/dev/null 2>&1 || {
  echo "npm is required to install Portless" >&2
  exit 2
}

python3 "$script_dir/preflight.py" --node-only
npm install --global portless@0.15.6
mkdir -p "$bin_dir"
cp "$script_dir/preflight.py" "$bin_dir/dotfactory-portless-preflight"
chmod +x "$bin_dir/dotfactory-portless-preflight"
"$bin_dir/dotfactory-portless-preflight"
