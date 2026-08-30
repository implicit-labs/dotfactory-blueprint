#!/usr/bin/env bash
set -euo pipefail

preflight="${DOTFACTORY_PORTLESS_PREFLIGHT:-dotfactory-portless-preflight}"
portless="${DOTFACTORY_PORTLESS_COMMAND:-portless}"
fixture_dir="$(mktemp -d)"
log_path="$fixture_dir/portless.log"
child_pid=""

cleanup() {
  if [ -n "$child_pid" ]; then
    kill "$child_pid" >/dev/null 2>&1 || true
    wait "$child_pid" >/dev/null 2>&1 || true
  fi
  rm -rf "$fixture_dir"
}
trap cleanup EXIT INT TERM

"$preflight" >/dev/null

(
  cd "$fixture_dir"
  "$portless" --name imp569-smoke python3 -c \
    'import http.server, os; http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), http.server.SimpleHTTPRequestHandler).serve_forever()'
) >"$log_path" 2>&1 &
child_pid=$!

attempt=0
url=""
while [ "$attempt" -lt 60 ]; do
  url="$(sed -n 's/.*PORTLESS_URL=\(https\?:\/\/[^ ]*\.localhost\).*/\1/p' "$log_path" | tail -1)"
  if [ -n "$url" ]; then
    break
  fi
  if ! kill -0 "$child_pid" >/dev/null 2>&1; then
    echo "Portless fixture exited before publishing its route" >&2
    exit 2
  fi
  sleep 0.25
  attempt=$((attempt + 1))
done

if [ -z "$url" ]; then
  echo "Portless fixture did not publish a .localhost URL" >&2
  exit 2
fi

curl --fail --silent --show-error "$url" >/dev/null
printf '%s\n' "$url"
