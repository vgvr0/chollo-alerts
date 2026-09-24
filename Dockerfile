FROM python:3.12-slim

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision=$VCS_REF

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
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["chollometro-alerts", "health"]

CMD ["chollometro-alerts", "run"]
