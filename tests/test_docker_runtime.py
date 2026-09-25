from pathlib import Path

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.repository import DealRepository

ROOT = Path(__file__).parents[1]


def test_compose_has_separate_listener_and_scanner_with_shared_storage():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "telegram:" in compose
    assert "scanner:" in compose
    assert compose.count("<<: *app") == 2
    assert "image: chollometro-alerts:runtime" in compose
    assert '["chollometro-alerts", "telegram-listen"]' in compose
    assert '["chollometro-alerts", "scan"]' in compose
    assert "DATABASE_PATH: /app/data/chollometro.sqlite3" in compose
    assert "- chollometro-data:/app/data" in compose
    assert compose.count("- chollometro-data:/app/data") == 1


def test_compose_scanner_health_is_local_and_restart_is_enabled():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert compose.count("restart: unless-stopped") == 1
    assert 'test: ["CMD", "chollometro-alerts", "health"]' in compose
    assert "https://" not in compose


def test_local_and_docker_database_paths_are_explicitly_separate():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "DATABASE_PATH=chollometro.sqlite3" in env_example
    assert "DATABASE_PATH: /app/data/chollometro.sqlite3" in compose
    assert "DATABASE_PATH=/app/data/chollometro.sqlite3" in dockerfile
    assert "DATABASE_PATH=/data/chollometro.sqlite3" not in dockerfile


def test_docker_build_is_lockfile_based_and_non_root():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "uv sync --frozen --no-dev" in dockerfile
    assert "USER appuser" in dockerfile
    assert "COPY .env" not in dockerfile
    assert "COPY . ." not in dockerfile


def test_dockerignore_excludes_secrets_databases_and_local_artifacts():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for entry in (".env", "*.sqlite3", ".git", ".venv", "__pycache__/"):
        assert entry in dockerignore


def test_compose_does_not_bake_credentials_into_configuration():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    for secret in ("TELEGRAM_BOT_TOKEN", "DEEPSEEK_API_KEY"):
        assert secret not in compose
        assert secret not in dockerfile
    assert "env_file:" in compose


def test_separate_runtime_commands_are_registered_in_cli_and_runtime():
    cli = (ROOT / "src" / "chollometro_alerts" / "cli.py").read_text(encoding="utf-8")
    runtime = (ROOT / "src" / "chollometro_alerts" / "runtime.py").read_text(
        encoding="utf-8"
    )

    assert 'scan_parser = sub.add_parser("scan"' in cli
    assert "run_scanner(service" in cli
    assert "def run_scanner(" in runtime


def test_listener_and_scanner_can_share_persistent_sqlite_state(tmp_path):
    path = tmp_path / "docker-data" / "chollometro.sqlite3"
    path.parent.mkdir()

    listener = DealRepository(path)
    user = listener.create_user(telegram_user_id="42", telegram_chat_id="chat")
    rule_id = listener.save_alert_rule(
        AlertRule(query="leche", constraints=AlertConstraints()),
        "leche",
        user_id=user.id,
    )
    listener.runtime_telegram_activity()
    listener.close_current_thread()

    scanner = DealRepository(path)
    assert scanner.get_rule(rule_id, user_id=user.id) is not None
    run_id = scanner.runtime_scan_started()
    scanner.runtime_scan_finished(run_id, "SUCCESS")
    scanner.close_current_thread()

    listener_after_restart = DealRepository(path)
    status = listener_after_restart.runtime_status()
    assert status["last_scan_run_id"] == run_id
    assert status["last_telegram_activity_at"] is not None
    listener_after_restart.close_current_thread()
