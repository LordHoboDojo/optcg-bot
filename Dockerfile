# The hosted site (optcg.server). Build from the project folder:
#     docker build -t optcg .
# Put the checkpoints the house bots use in models/ first (e.g. cp runs/league/latest.pt models/house.pt)
# and name them in OPTCG_BOTS (e.g. "greedy;models/house.pt;mcts:models/house.pt").

# The Rust engine, as a wheel
FROM python:3.10-slim AS engine
RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential && rm -rf /var/lib/apt/lists/*
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH=/root/.cargo/bin:$PATH
RUN pip install --no-cache-dir "maturin>=1.5,<2.0"
COPY engine-rs /src/engine-rs
RUN maturin build --release -m /src/engine-rs/Cargo.toml -o /wheels

# The React site, built to static files
FROM node:22-slim AS site
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web ./
RUN npm run build

FROM python:3.10-slim
# PyTorch is only needed for network bots (.pt, mcts:); set WITH_TORCH=0 for random/greedy only.
ARG WITH_TORCH=1
ARG TORCH_VERSION=2.14.1
RUN pip install --no-cache-dir --upgrade pip
# torch from PyTorch's CPU-only index; its dependencies from PyPI
RUN if [ "$WITH_TORCH" = "1" ]; then \
      pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple "torch==${TORCH_VERSION}"; fi
COPY requirements-server.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-server.txt
COPY --from=engine /wheels /tmp/wheels
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels

WORKDIR /app
COPY optcg optcg
COPY data data
COPY decks decks
COPY docs docs
COPY models models
COPY --from=site /web/dist web/dist
RUN useradd --create-home optcg && mkdir -p /data && chown optcg /data
USER optcg
ENV OPTCG_DB=/data/server.db OPTCG_REPLAYS=/data/replays PORT=8000
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["python", "-m", "optcg.server", "--host", "0.0.0.0"]
