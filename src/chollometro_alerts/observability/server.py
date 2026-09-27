"""Minimal HTTP server for liveness, readiness, health and Prometheus."""

from __future__ import annotations

import json
import logging
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import exposition

from ..config import ConfigurationError

logger = logging.getLogger(__name__)


def metrics_port() -> int:
    raw = os.getenv("METRICS_PORT", "8000")
    try:
        port = int(raw)
    except ValueError as exc:
        raise ConfigurationError("METRICS_PORT debe ser un entero") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("METRICS_PORT debe estar entre 1 y 65535")
    return port


class _Handler(BaseHTTPRequestHandler):
    server_version = "chollo-alerts-observability/1"

    def do_GET(self):
        if self.path == "/health/live":
            self._json({"status": "ok"})
            return
        if self.path == "/health/ready":
            try:
                self.server.ready_check()
            except Exception:  # noqa: BLE001 - readiness must return 503
                self._json({"status": "not_ready", "database": "error"}, 503)
            else:
                self._json({"status": "ready", "database": "ok"})
            return
        if self.path == "/health":
            try:
                self.server.ready_check()
            except Exception:  # noqa: BLE001 - health exposes local dependency state
                self._json(self.server.state.health_payload(database="error"), 503)
            else:
                self._json(self.server.state.health_payload())
            return
        if self.path == "/metrics":
            body, content_type = (
                exposition.generate_latest(self.server.state.registry),
                exposition.CONTENT_TYPE_LATEST,
            )
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _json(self, payload, status=200):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.info("http.%s", format % args)


class ObservabilityServer:
    def __init__(self, state, repository, host="0.0.0.0", port=None):
        self.state = state
        self.repository = repository
        self.httpd = ThreadingHTTPServer(
            (host, metrics_port() if port is None else port), _Handler
        )
        self.httpd.state = state
        self.httpd.ready_check = self._ready_check
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="observability-http",
            daemon=True,
        )

    def _ready_check(self):
        # A local SQLite query is cheap and does not contact Telegram or the provider.
        self.repository.runtime_status()

    def start(self):
        self.thread.start()
        logger.info("observability.started host=%s port=%s", *self.httpd.server_address)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        logger.info("observability.stopped")
