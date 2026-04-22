FROM python:3.11.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    asyncpg==0.30.0 \
    httpx==0.28.1 \
    websockets==14.1 \
    pydantic==2.10.4 \
    pydantic-settings==2.7.0 \
    tenacity==9.0.0 \
    aiolimiter==1.2.1 \
    structlog==24.4.0 \
    orjson==3.10.12

COPY src ./src
ENV PYTHONPATH=/app/src

CMD ["python", "-m", "trading.cli.ingestor"]
