#!/usr/bin/env bash
set -euo pipefail

preflight="${DOTFACTORY_PORTLESS_PREFLIGHT:-dotfactory-portless-preflight}"
portless="${DOTFACTORY_PORTLESS_COMMAND:-portless}"
fixture_dir="$(mktemp -d)"
log_path="$fixture_dir/portless.log"
child_pid=""
service="dotfactory-smoke-$$"

stop_child() {
  if [ -n "$child_pid" ]; then
    kill "$child_pid" >/dev/null 2>&1 || true
    wait "$child_pid" >/dev/null 2>&1 || true
    child_pid=""
  fi
}

cleanup() {
  stop_child
  rm -rf "$fixture_dir"
}
trap cleanup EXIT INT TERM

"$preflight" >/dev/null
url="$("$portless" get "$service")"
case "$url" in
  https://*.localhost) ;;
  *)
    echo "Portless returned a non-loopback fixture URL" >&2
    exit 2
    ;;
esac

(
  DOTFACTORY_FIXTURE_DIR="$fixture_dir" \
    "$portless" run --name "$service" python3 -u -c \
    'import functools, http.server, os; handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=os.environ["DOTFACTORY_FIXTURE_DIR"]); http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), handler).serve_forever()'
) >"$log_path" 2>&1 &
child_pid=$!

attempt=0
while [ "$attempt" -lt 60 ]; do
  if curl --fail --silent --show-error "$url" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$child_pid" >/dev/null 2>&1; then
    echo "Portless fixture exited before publishing its route" >&2
    sed -n '1,120p' "$log_path" >&2
    exit 2
  fi
  sleep 0.25
  attempt=$((attempt + 1))
done

if [ "$attempt" -eq 60 ]; then
  echo "Portless fixture route did not become reachable" >&2
  sed -n '1,120p' "$log_path" >&2
  exit 2
fi

stop_child
attempt=0
while [ "$attempt" -lt 40 ]; do
  if ! "$portless" list 2>/dev/null | grep -F "$url" >/dev/null; then
    break
  fi
  sleep 0.25
  attempt=$((attempt + 1))
done
if [ "$attempt" -eq 40 ]; then
  echo "Owned Portless fixture route remained after cleanup" >&2
  exit 2
fi

printf '%s\n' "$url"
