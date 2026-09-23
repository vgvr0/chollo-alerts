FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/chollometro.sqlite3

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["chollometro-alerts", "health"]

CMD ["chollometro-alerts", "run"]
