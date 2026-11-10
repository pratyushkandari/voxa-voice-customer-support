#!/usr/bin/env bash
# Updates a Docker Compose deployment to origin/main and rebuilds the app images.
# Run it on the server from the repository root: bash scripts/deploy.sh [--force]
# CI runs the same script over SSH after the tests pass on main (docs/RUNBOOK.md).
#
# The server's disk can't hold the old and new images at once, so the app is stopped and its
# images and build cache are removed before the build. The site is down until the build
# finishes (10-25 minutes). PostgreSQL, Redis and Prometheus keep running.
#
# Everything is inside main() so bash reads the whole script before `git reset` can
# replace this file.
set -euo pipefail

main() {
  cd "$(dirname "$0")/.."
  local app_services="migrate gateway stt-worker llm-worker tts-worker worker-supervisor frontend"
  local deployed_file="$HOME/.voxa-deployed-commit"

  # One deploy at a time.
  exec 9>/tmp/voxa-deploy.lock
  flock -n 9 || { echo "Another deploy is running; try again later."; exit 1; }

  git fetch --quiet origin main
  git reset --quiet --hard origin/main
  local commit
  commit=$(git rev-parse --short HEAD)

  # Skip the downtime when this commit is already deployed and running.
  if [[ "${1:-}" != "--force" && -f "$deployed_file" && "$(cat "$deployed_file")" == "$commit" ]] \
    && curl -sf http://localhost:8088/health > /dev/null; then
    echo "$commit is already deployed and healthy; nothing to do (use --force to rebuild)."
    exit 0
  fi
  echo "Deploying $(git log --oneline -1)"

  # Keep a database backup before migrations run (the last 5 are kept).
  mkdir -p "$HOME/backups"
  if docker ps --format '{{.Names}}' | grep -qx voxa-postgres; then
    docker exec voxa-postgres pg_dump -U postgres voxa_db \
      | gzip > "$HOME/backups/voxa_db_$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
    ls -1t "$HOME"/backups/voxa_db_*.sql.gz | tail -n +6 | xargs -r rm --
  fi

  # Free the space the new images need: stop the app, then remove its images and the build cache.
  # shellcheck disable=SC2086
  docker compose rm --stop --force $app_services
  local project
  project=$(docker compose config --format json | jq -r .name)
  docker image ls -q --filter "label=com.docker.compose.project=$project" | xargs -r docker image rm -f > /dev/null
  docker image prune -f > /dev/null
  docker builder prune -af > /dev/null
  echo "Free disk before the build: $(df -h --output=avail / | tail -1 | tr -d ' ')"

  trap 'echo "The deploy failed and the app is stopped. Fix the cause, then run: bash scripts/deploy.sh --force"' ERR

  # One image at a time: building them in parallel runs a 4 GB VM out of memory.
  for service in $app_services; do
    echo "Building $service"
    docker compose build --quiet "$service"
  done
  docker compose up -d --remove-orphans
  docker builder prune -af > /dev/null

  for _ in $(seq 1 60); do
    if curl -sf http://localhost:8088/health > /dev/null; then
      echo "$commit" > "$deployed_file"
      echo "Deployed $commit; the gateway is healthy. Free disk: $(df -h --output=avail / | tail -1 | tr -d ' ')"
      exit 0
    fi
    sleep 5
  done
  echo "The gateway did not become healthy within 5 minutes."
  docker compose ps
  docker compose logs --tail 30 migrate gateway
  exit 1
}

main "$@"
