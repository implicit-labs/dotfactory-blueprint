#!/usr/bin/env bash
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fixture_dir="$(mktemp -d)"
trap 'rm -rf "$fixture_dir"' EXIT

mkdir -p "$fixture_dir/bin"
cp "$skill_dir/scripts/preflight.py" "$fixture_dir/preflight.py"

printf '%s\n' '#!/usr/bin/env bash' \
  'case "${1:-}" in' \
  '  --version) echo 0.15.6 ;;' \
  '  doctor) echo "Summary: 0 failures, 0 warnings." ;;' \
  'esac' > "$fixture_dir/bin/portless"
printf '%s\n' '#!/usr/bin/env bash' 'echo v24.3.0' > "$fixture_dir/bin/node"
chmod +x "$fixture_dir/bin/portless" "$fixture_dir/bin/node"

PATH="$fixture_dir/bin:$PATH" python3 "$fixture_dir/preflight.py" | grep -q '"ok": true'

printf '%s\n' '#!/usr/bin/env bash' \
  'case "${1:-}" in' \
  '  --version) echo 0.15.5 ;;' \
  '  doctor) echo "Summary: 0 failures, 0 warnings." ;;' \
  'esac' > "$fixture_dir/bin/portless"
chmod +x "$fixture_dir/bin/portless"
if PATH="$fixture_dir/bin:$PATH" python3 "$fixture_dir/preflight.py" >/dev/null; then
  echo "preflight accepted the wrong Portless version" >&2
  exit 1
fi

printf '%s\n' '#!/usr/bin/env bash' 'echo v22.23.1' > "$fixture_dir/bin/node"
chmod +x "$fixture_dir/bin/node"
if PATH="$fixture_dir/bin:$PATH" python3 "$fixture_dir/preflight.py" --node-only >/dev/null; then
  echo "preflight accepted an unsupported Node runtime" >&2
  exit 1
fi

npm_marker="$fixture_dir/npm-called"
printf '%s\n' '#!/usr/bin/env bash' ': > "$DOTFACTORY_NPM_MARKER"' > "$fixture_dir/bin/npm"
chmod +x "$fixture_dir/bin/npm"
if DOTFACTORY_NPM_MARKER="$npm_marker" \
  DOTFACTORY_BIN_DIR="$fixture_dir/install-bin" \
  PATH="$fixture_dir/bin:$PATH" \
  /bin/bash "$skill_dir/scripts/install.sh" >/dev/null 2>&1; then
  echo "installer accepted an unsupported Node runtime" >&2
  exit 1
fi
if [ -e "$npm_marker" ]; then
  echo "installer mutated npm before checking Node" >&2
  exit 1
fi

printf '%s\n' '#!/usr/bin/env bash' 'echo v24.3.0' > "$fixture_dir/bin/node"
printf '%s\n' '#!/usr/bin/env bash' \
  'case "${1:-}" in' \
  '  --version) echo 0.15.6 ;;' \
  '  doctor) echo "Summary: 0 failures, 1 warning." ;;' \
  'esac' > "$fixture_dir/bin/portless"
chmod +x "$fixture_dir/bin/portless" "$fixture_dir/bin/node"
if PATH="$fixture_dir/bin:$PATH" python3 "$fixture_dir/preflight.py" >/dev/null; then
  echo "preflight accepted an interactive-readiness warning" >&2
  exit 1
fi

if [ "${DOTFACTORY_PORTLESS_LIVE:-0}" = "1" ]; then
  "$skill_dir/scripts/smoke.sh"
fi
