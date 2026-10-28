#!/usr/bin/env bash
# Wait until a CamusDB container answers /ping, and fail with its logs if it does not.
#
# Usage: wait-for-camusdb.sh <container> <base-url>
#
# Kestrel binds the port before startup finishes, so a server that fails during startup can answer
# /ping for a few seconds and then exit. Thus a ping is necessary but not sufficient: the script
# checks again, a moment later, that the container still runs.
set -euo pipefail

container="$1"
url="$2"

running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" = "true" ]
}

for i in $(seq 1 90); do
  if ! running; then
    echo "The CamusDB container exited during startup." >&2
    docker logs "$container" || true
    exit 1
  fi
  if curl --fail --silent --max-time 2 "$url/ping" >/dev/null 2>&1; then
    sleep 3
    if ! running; then
      echo "CamusDB answered /ping and then exited: it failed during startup." >&2
      docker logs "$container" || true
      exit 1
    fi
    echo "CamusDB is up after ${i}s."
    exit 0
  fi
  sleep 1
done

echo "CamusDB did not become ready in time." >&2
docker logs "$container" || true
exit 1
