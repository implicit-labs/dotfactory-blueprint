#!/usr/bin/env bash
set -euo pipefail

# Requires a local Docker daemon. Tests a container volume, not Render's mount.
worker_image="dotfactory-worker-test:local"
worker_container="dotfactory-worker-test-$$"
worker_volume="dotfactory-worker-test-$$"
cleanup() {
  docker rm -f "$worker_container" >/dev/null 2>&1 || true
  docker volume rm "$worker_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker build -t "$worker_image" -f factory/deploy/owned-worker/Dockerfile .
docker volume create "$worker_volume" >/dev/null
docker run -d --name "$worker_container" --mount "type=volume,src=$worker_volume,dst=/data" "$worker_image" >/dev/null
docker exec "$worker_container" sh -c 'test "$(id -u)" = 10001 && test "$(id -g)" = 10001 && test -d "$HOME/.ssh"'
docker exec "$worker_container" codex --version
docker exec "$worker_container" claude --version
docker exec "$worker_container" python3 -m dotfactory --help
docker exec "$worker_container" bash -c '
  set -euo pipefail
  git init --bare /data/repositories/smoke.git
  git clone /data/repositories/smoke.git /data/repositories/smoke
  cd /data/repositories/smoke
  git config user.name "Image test"
  git config user.email "image-test@example.invalid"
  git checkout -b main
  echo "print(1)" > smoke.py
  git add smoke.py
  git commit -m "Image test seed"
  git push origin main
  python3 -m dotfactory init --repository /data/repositories/smoke --output /data/instances/smoke --project smoke --linear-project image-test
'
docker exec "$worker_container" python3 -c 'import importlib.util; s=importlib.util.spec_from_file_location("start", "/opt/dotfactory/start.py"); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert m.coordinator_command({"DOTFACTORY_CONFIG":"/data/instances/smoke/factory.json", "DOTFACTORY_PROJECT":"smoke"})'
docker exec "$worker_container" python3 -c 'from pathlib import Path; Path("/data/restart-proof").write_text("preserved")'
docker restart "$worker_container" >/dev/null
docker exec "$worker_container" python3 -c 'from pathlib import Path; assert Path("/data/restart-proof").read_text() == "preserved"'
docker exec "$worker_container" python3 -c 'from dotfactory.instance import FactoryConfig; c=FactoryConfig.load("/data/instances/smoke/factory.json"); assert c.resolve_workflow("smoke")'
docker exec "$worker_container" python3 -c 'import os; assert os.environ["CODEX_HOME"] == "/data/codex"; assert os.environ["CLAUDE_CONFIG_DIR"] == "/data/claude"'
docker logs "$worker_container"
