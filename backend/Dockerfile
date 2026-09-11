# The consolidation image: one stage per `docker run` (see scripts/stage.py), the same image locally and on
# Cloudflare Containers. Nothing secret is baked in; ADMIN_TOKEN, R2_*, OPENAI_KEY, and SLACK_RUN_WEBHOOK arrive as
# environment variables at run time (locally: --env-file backend/.dev.vars via scripts/container-run.sh).
#
#   docker build -f backend/Dockerfile -t open-jobs-consolidate .        # from the repo root: native arch
#   docker build -f backend/Dockerfile --platform linux/amd64 -t ... .    # what Cloudflare runs
#
# Layout inside: /app = backend/ (scripts, src/boards.json); /work = scratch (work-<date>/, export/diffs, export/ledger,
# export/feed); mount a volume there so a failed stage can be re-run without redoing earlier ones.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_CACHE_DIR=/opt/uv-cache \
    NODE_VERSION=22 HOME=/root
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg xz-utils bc procps \
 && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# scripts, the board list, and the tools/ helpers the estimators import; no exports, no logs, no secrets
# (build context is the repo root; see /.dockerignore)
COPY backend/scripts/ /app/scripts/
COPY backend/src/boards.json /app/src/boards.json
COPY backend/package.json /app/package.json
COPY tools/*.py tools/*.json /tools/

# Pre-resolve every script's inline dependencies into the image so the first run is not a download, and fetch
# DuckDB's httpfs extension (read_parquet over S3) so a stage never needs the extension repo at run time.
RUN for s in stage build-parquet build-diff build-ledger build-manifest publish-web upload-history \
             train-salary train-arrangement train-seniority train-age build-city-table build-location-table \
             build-job-changes build-archive; do uv sync --script /app/scripts/$s.py || exit 1; done \
 && uv run --with duckdb python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs'); print('httpfs installed')"

# scratch and state
RUN mkdir -p /work && ln -s /work/export /app/export && ln -s /work/logs /app/logs
VOLUME ["/work"]
# DuckDB caps inside the image: the VM is 16 GB and a 20 GB default let the diff get OOM-killed (exit 137) on 2026-09-10
# LOW_DISK / STAGE_TO_BUCKET: the image behaves as it must on a 20 GB cloud instance, on the laptop too, so the
# cloud path is exercised every night (pass -e LOW_DISK=0 -e STAGE_TO_BUCKET=0 to opt out)
ENV CONSOLIDATE_SOURCE=r2 WORK_ROOT=/work DIFF_MEMORY=10GB FEED_DUCKDB_MEMORY=2GB PARQUET_MEMORY=6GB PARQUET_PART_ROWS=2000000 LOW_DISK=1 STAGE_TO_BUCKET=1
ENTRYPOINT ["uv", "run", "--script", "/app/scripts/stage.py"]
CMD ["--help"]
