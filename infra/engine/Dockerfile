FROM python:3.11.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# Keep the core runtime deps in sync with pyproject.toml [project.dependencies].
# Nautilus lives in the `engine-live` extra and is NOT installed in the
# Phase 2 image (see ADR 0006). Phase 3+ will build a separate image layer
# that installs the extra.
RUN pip install --no-cache-dir \
    asyncpg==0.30.0 \
    httpx==0.28.1 \
    websockets==14.1 \
    pydantic==2.10.4 \
    pydantic-settings==2.7.0 \
    tenacity==9.0.0 \
    aiolimiter==1.2.1 \
    structlog==24.4.0 \
    orjson==3.10.12 \
    numpy==2.2.1 \
    pandas==2.2.3 \
    scikit-learn==1.6.0 \
    lightgbm==4.5.0 \
    optuna==4.1.0 \
    psycopg2-binary==2.9.10 \
    jinja2==3.1.5 \
    plotly==5.24.1 \
    tomli==2.2.1 \
    redis==5.2.1

COPY src ./src
COPY config ./config
ENV PYTHONPATH=/app/src

# Read-only mount of polybot-btc5m SQLite is attached from docker-compose
# for parity tests (see docker-compose.yml:tea-engine.volumes).

CMD ["python", "-m", "trading.cli.paper_engine"]
