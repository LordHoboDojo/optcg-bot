#!/bin/sh
# Runs on the server: puts the image of one commit live, and goes back to what was running if
# the new one doesn't come up healthy. Called by .github/workflows/deploy.yml.
#     deploy/deploy.sh <commit sha>
set -eu
cd "$(dirname "$0")/.."

image=ghcr.io/lordhobodojo/optcg-bot:$1
domain=$(sed -n 's/^OPTCG_DOMAIN=//p' .env)
previous=$(docker compose ps -q app | xargs -r docker inspect --format '{{.Config.Image}}')

up() {
  OPTCG_IMAGE=$1 docker compose up -d --no-build --wait --wait-timeout 180 &&
    curl --fail --silent --show-error --output /dev/null --max-time 10 \
      --retry 5 --retry-all-errors --retry-delay 3 "https://$domain/healthz"
}

docker pull --quiet "$image"
if ! up "$image"; then
  echo "$image did not come up healthy" >&2
  docker compose logs --tail 50 app >&2
  if [ -n "$previous" ]; then
    echo "going back to $previous" >&2
    up "$previous"
  fi
  exit 1
fi

# Picks up a changed Caddyfile; a bad one is refused and the old one keeps running
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
# Rolling back further than a week pulls the image again
docker image prune --all --force --filter until=168h > /dev/null
