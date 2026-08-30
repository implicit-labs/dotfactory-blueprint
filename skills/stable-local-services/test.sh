#!/usr/bin/env bash
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fixture_dir="$(mktemp -d)"
trap 'rm -rf "$fixture_dir"' EXIT

mkdir -p "$fixture_dir/bin"
cp "$skill_dir/scripts/preflight.py" "$fixture_dir/preflight.py"

printf '%s\n' '#!/usr/bin/env bash' 'echo 0.15.6' > "$fixture_dir/bin/portless"
printf '%s\n' '#!/usr/bin/env bash' 'echo v24.3.0' > "$fixture_dir/bin/node"
chmod +x "$fixture_dir/bin/portless" "$fixture_dir/bin/node"

PATH="$fixture_dir/bin:$PATH" python3 "$fixture_dir/preflight.py" | grep -q '"ok": true'

printf '%s\n' '#!/usr/bin/env bash' 'echo 0.15.5' > "$fixture_dir/bin/portless"
chmod +x "$fixture_dir/bin/portless"
if PATH="$fixture_dir/bin:$PATH" python3 "$fixture_dir/preflight.py" >/dev/null; then
  echo "preflight accepted the wrong Portless version" >&2
  exit 1
fi

if [ "${DOTFACTORY_PORTLESS_LIVE:-0}" = "1" ]; then
  "$skill_dir/scripts/smoke.sh"
fi
