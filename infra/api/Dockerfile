FROM python:3.11.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    asyncpg==0.30.0 \
    httpx==0.28.1 \
    pydantic==2.10.4 \
    pydantic-settings==2.7.0 \
    structlog==24.4.0 \
    orjson==3.10.12 \
    tomli==2.2.1 \
    redis==5.2.1 \
    fastapi==0.115.6 \
    uvicorn[standard]==0.34.0 \
    jinja2==3.1.5 \
    python-multipart==0.0.20 \
    itsdangerous==2.2.0 \
    numpy==2.2.1 \
    pandas==2.2.3 \
    plotly==5.24.1

COPY src ./src
COPY config ./config
ENV PYTHONPATH=/app/src

EXPOSE 8000
CMD ["uvicorn", "trading.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
