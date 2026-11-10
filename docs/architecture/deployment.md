# Deployment

The site runs as one Docker container behind Caddy on a single small server, deployed
automatically when CI passes on `main`. Card art is served from S3 through CloudFront. The
step-by-step setup (server, domain, Google sign-in, art upload, house bots, logs, backups) is in
[`docs/hosting.md`](../hosting.md); this page shows how the pieces fit.

Configuration is by environment variable, in a `.env` file on the server (`.env.example` lists
every variable). This documentation names variables only, never their values.

## Production layout

```mermaid
flowchart LR
    user(("Browser")) -- "HTTPS 443" --> caddy
    botc(("Remote bot")) -- "WSS 443" --> caddy
    user -- "card art" --> cf["CloudFront"]
    cf --> s3[("S3 bucket<br/>Cards/, CardBacks/,<br/>Playmats/, background.jpg")]
    subgraph server["Server (Docker, docker-compose.yml)"]
        caddy["caddy:2.8<br/>deploy/Caddyfile<br/>TLS for OPTCG_DOMAIN, gzip"]
        app["app: optcg image<br/>python -m optcg.server :8000"]
        vdata[("volume data: /data<br/>server.db, replays/")]
        vcaddy[("volume caddy: certificates")]
        models[/"./models (read-only)<br/>house bot checkpoints, art.json"/]
        env[/".env"/]
        caddy -- "reverse_proxy app:8000" --> app
        app --- vdata
        caddy --- vcaddy
        app --- models
        env -.-> app
    end
    app -. "JSON logs (optional awslogs driver)" .-> cw["CloudWatch Logs"]
    google["Google OAuth"] <-. "sign-in redirects" .-> user
```

- **Caddy** (`deploy/Caddyfile`) gets and renews the certificate for `OPTCG_DOMAIN`, compresses
  responses and proxies everything, WebSockets included, to `app:8000`.
- **The app container** runs as an unprivileged user, with `OPTCG_DB=/data/server.db`,
  `OPTCG_REPLAYS=/data/replays` and `PORT=8000` set in the image, and
  `OPTCG_SECURE_COOKIES=1` from the compose file. Its health check calls `/healthz`.
- **`./models`** is mounted read-only at `/app/models`: the house bots' checkpoints (named in
  `OPTCG_BOTS`) and the art manifest. Changing a bot takes `docker compose restart app`, not a
  deploy.
- **Card art** is not in the image. With `OPTCG_ART_BASE` set to the CloudFront URL and
  `OPTCG_ART_MANIFEST` pointing at the art list (made by `python -m optcg.server --art-manifest`),
  the table and the deck builder load images straight from CloudFront. Without them, the server
  serves art from a local `StreamingAssets/` folder at `/art/`, or the table draws text cards.
- **Logs** are one JSON object per line on stdout (`optcg/server/logs.py`); the commented
  `awslogs` block in `docker-compose.yml` sends them to CloudWatch.
- Live browser games are in the app's memory, so there is one app container, and a deploy ends
  the games in progress.

## The Docker image

```mermaid
flowchart TB
    subgraph s1["stage engine: python:3.10-slim"]
        e1["rustup (minimal) + maturin"] --> e2["maturin build --release<br/>engine-rs to a wheel"]
    end
    subgraph s2["stage site: node:22-slim"]
        n1["npm ci"] --> n2["npm run build: web/dist"]
    end
    subgraph s3["final: python:3.10-slim"]
        f1["torch (CPU-only index), unless WITH_TORCH=0"] --> f2["requirements-server.txt"]
        f2 --> f3["install the optcg_rs wheel"]
        f3 --> f4["copy optcg/, data/, decks/, docs/, web/dist"]
        f4 --> f5["user optcg, /data<br/>HEALTHCHECK /healthz<br/>CMD python -m optcg.server --host 0.0.0.0"]
    end
    e2 --> f3
    n2 --> f4
```

`docs/` is copied in because the site serves `docs/bot-api.md` at `/api/docs/bot-api`.

## CI

`.github/workflows/ci.yml` runs on every pull request and every push to `main` (a newer push to
the same branch cancels the run in progress). Nothing is deployed from it.

| Job | What it does |
|---|---|
| Python tests | Python 3.10, CPU PyTorch, the server requirements, builds and installs the Rust engine wheel, `python -m unittest discover tests` |
| Rust tests | `cargo test --release` in `engine-rs/` (also builds the examples) |
| Site build | `npm ci` and `npm run build` in `web/` (typecheck and bundle) |
| Docker image | Builds the image without pushing it |

The Playwright browser tests are not in CI; they run locally (`npm run test:e2e` in `web/`).

## Automatic deploy

`.github/workflows/deploy.yml` runs when the CI workflow completes on `main`, or by hand with a
commit SHA (to roll back). It deploys only after CI succeeded on a **push** to this repository,
never for a pull request or a fork.

```mermaid
sequenceDiagram
    actor Dev as Developer
    participant GH as GitHub (main)
    participant CI as CI workflow
    participant D as Deploy workflow
    participant R as GHCR image registry
    participant Srv as Server (over SSH)
    participant DS as deploy/deploy.sh
    Dev->>GH: merge a PR into main
    GH->>CI: push to main
    alt CI fails
        CI-->>D: completed, conclusion failure
        D->>D: skipped (the job's if is false)
    else CI succeeds
        CI-->>D: workflow_run completed, success, event push
        D->>D: check the SHA is 40 hex characters
        D->>R: build and push optcg-bot:SHA (GitHub Actions cache)
        D->>Srv: SSH with DEPLOY_SSH_KEY and DEPLOY_KNOWN_HOSTS to DEPLOY_HOST
        D->>Srv: scp docker-compose.yml, Caddyfile, deploy.sh
        D->>Srv: docker login ghcr.io (GITHUB_TOKEN via stdin)
        D->>DS: deploy.sh SHA
        DS->>R: docker pull optcg-bot:SHA
        DS->>DS: remember the image running now
        DS->>Srv: docker compose up -d --wait (up to 180 s)
        DS->>Srv: curl https://OPTCG_DOMAIN/healthz (5 retries)
        alt healthy
            DS->>Srv: caddy reload (a bad Caddyfile is refused, the old one stays)
            DS->>Srv: prune images older than a week
            DS-->>D: exit 0
            D->>Srv: docker logout ghcr.io
            D->>R: tag SHA as latest
        else not healthy
            DS->>Srv: print the last 50 app log lines
            DS->>Srv: compose up the previous image again
            DS-->>D: exit 1
            D->>Srv: docker logout ghcr.io (always)
            D-->>Dev: the workflow fails, latest does not move
        end
    end
```

- Deploys run one at a time and are never cancelled halfway (`concurrency: deploy`).
- The job uses the GitHub environment `production`, which holds the secrets `DEPLOY_SSH_KEY` and
  `DEPLOY_KNOWN_HOSTS` and the variables `DEPLOY_HOST` and `SITE_URL`.
- **Rollback**: Actions, Deploy, Run workflow with an older commit's full SHA. A manual run
  skips the build and deploys the image already in the registry for that SHA.
- The site is down for as long as the new container takes to start, and live games end, so
  merging to `main` is what restarts the site.

## Running it locally

```bash
.venv/bin/pip install -r requirements-server.txt
(cd web && npm install && npm run build)
OPTCG_DEV_LOGIN=1 .venv/bin/python -m optcg.server     # http://127.0.0.1:8000
```

The Rust engine must be built into the virtualenv first (see
[`engine-rs/README.md`](../../engine-rs/README.md)). `docker compose up -d --build` runs the
production layout locally, with a `.env` made from `.env.example`.
