# Hosting

One small server runs the site in Docker behind Caddy (HTTPS). Card art sits in S3 behind
CloudFront, and logs go to CloudWatch. Browser games live in the server's memory, so run one
copy of the app: a restart or deploy ends the games in progress. Accounts, bots, keys and
matches are kept in SQLite on a Docker volume.

## Running it locally

```bash
.venv/bin/pip install -r requirements-server.txt
(cd web && npm install && npm run build)                    # the pages, into web/dist
OPTCG_DEV_LOGIN=1 .venv/bin/python -m optcg.server          # http://127.0.0.1:8000
OPTCG_KEY=optcg_... .venv/bin/python tools/bot_client.py -v  # a key from Your bots
```

While working on the pages, run `npm run dev` in `web/` as well and open http://127.0.0.1:5173:
it reloads on every change and passes the API through to the server on :8000.

The Analysis page reads `data/analysis.json`. After training new checkpoints, refresh it
(about a minute) and commit it:

```bash
.venv/bin/python tools/analysis.py --games 400
```

`OPTCG_DEV_LOGIN=1` adds a "Sign in (dev)" link that needs no Google account. Never set it on a
public server. Every setting is in `.env.example`.

## On AWS

1. **A server.** A Lightsail instance (2 GB, Ubuntu) or an EC2 `t4g.small`. Open ports 80 and
   443, and give it a static IP. Install Docker: `curl -fsSL https://get.docker.com | sh`.
2. **A domain.** Point an A record (Route 53 or any DNS) at the static IP.
3. **Google sign-in** (optional). In Google Cloud Console, create an OAuth client ID of type
   Web application with the redirect URI `https://<domain>/auth/google`. Put its ID and secret
   in `.env`.
4. **Card art** (optional). Upload the images and a list of them:
   ```bash
   aws s3 sync StreamingAssets/Cards s3://<bucket>/Cards --exclude "*_small*"
   aws s3 sync StreamingAssets/CardBacks s3://<bucket>/CardBacks
   aws s3 sync StreamingAssets/Playmats s3://<bucket>/Playmats
   aws s3 cp StreamingAssets/background.jpg s3://<bucket>/
   .venv/bin/python -m optcg.server --art-manifest models/art.json
   ```
   Put a CloudFront distribution in front of the bucket, then set `OPTCG_ART_BASE` to its URL
   and `OPTCG_ART_MANIFEST=/app/models/art.json`.
5. **The house bots.** Copy the checkpoints into `models/` on the server (for example
   `scp runs/league/latest.pt server:optcg/models/house.pt`) and name them in `OPTCG_BOTS`. The
   folder is mounted into the container, so changing a bot takes `docker compose restart app`,
   not a deploy.
6. **First deploy.** Copy the project to the server (or `git pull` there), then:
   ```bash
   cp .env.example .env    # fill it in
   docker compose up -d --build
   ```
   Caddy gets the certificate on the first request. `docker compose logs -f app` shows the logs.
   After this, deploys are automatic (see below).
7. **Logs** (optional). Give the instance an IAM role allowed to write CloudWatch Logs and
   uncomment the `awslogs` block in `docker-compose.yml`. Each line is a JSON object, so
   CloudWatch Logs Insights can query them, for example:
   ```
   fields @timestamp, bot, reason | filter event = "match_over" and reason != "normal"
   ```
   Worth an alarm: `request_failed` (an exception) and `illegal_move` rates.

## Deploys

When CI passes on `main`, `.github/workflows/deploy.yml` builds the image, pushes it to
`ghcr.io/lordhobodojo/optcg-bot` tagged with the commit, and runs `deploy/deploy.sh` on the
server. If the new container isn't healthy, or the site doesn't answer through Caddy, the script
goes back to the image that was running and the workflow fails. Only then does `latest` move.

The site is down for about as long as the new container takes to start, and games in progress
end, so merging to `main` is what restarts the site.

To go back to an older commit: Actions > Deploy > Run workflow, with that commit's full SHA.

The workflow needs a GitHub environment `production`, limited to protected branches, with:

- `DEPLOY_SSH_KEY`: a private key whose public half is in the server's `authorized_keys`.
- `DEPLOY_KNOWN_HOSTS`: the server's line(s) from `known_hosts`.

and the variables `DEPLOY_HOST` (`ubuntu@<ip>`) and `SITE_URL`.

## Backups

`/data` (the `data` volume) holds `server.db` and the match replays. Copy it off the server now
and then, e.g. `docker compose cp app:/data ./backup` from cron, then to S3.
