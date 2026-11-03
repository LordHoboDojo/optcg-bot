# Hosting

One small server runs the site in Docker behind Caddy (HTTPS). Card art sits in S3 behind
CloudFront, and logs go to CloudWatch. Browser games live in the server's memory, so run one
copy of the app: a restart or deploy ends the games in progress. Accounts, bots, keys and
matches are kept in SQLite on a Docker volume.

## Running it locally

```bash
.venv/bin/pip install -r requirements-server.txt
OPTCG_DEV_LOGIN=1 .venv/bin/python -m optcg.server          # http://127.0.0.1:8000
OPTCG_KEY=optcg_... .venv/bin/python tools/bot_client.py -v  # a key from Your bots
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
5. **The house bots.** Copy the checkpoints into `models/` (for example
   `cp runs/league/latest.pt models/house.pt`) and name them in `OPTCG_BOTS`.
6. **Deploy.** Copy the project to the server (or `git pull` there), then:
   ```bash
   cp .env.example .env    # fill it in
   docker compose up -d --build
   ```
   Caddy gets the certificate on the first request. `docker compose logs -f app` shows the logs.
7. **Logs** (optional). Give the instance an IAM role allowed to write CloudWatch Logs and
   uncomment the `awslogs` block in `docker-compose.yml`. Each line is a JSON object, so
   CloudWatch Logs Insights can query them, for example:
   ```
   fields @timestamp, bot, reason | filter event = "match_over" and reason != "normal"
   ```
   Worth an alarm: `request_failed` (an exception) and `illegal_move` rates.

## Backups

`/data` (the `data` volume) holds `server.db` and the match replays. Copy it off the server now
and then, e.g. `docker compose cp app:/data ./backup` from cron, then to S3.
