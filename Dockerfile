FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/chollometro.sqlite3

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["chollometro-alerts", "health"]

ENTRYPOINT ["chollometro-alerts"]
CMD ["run"]
