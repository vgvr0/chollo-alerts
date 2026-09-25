import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from dotenv import dotenv_values

from chollometro_alerts import cli, config


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL",
        "LLM_ENABLED",
        "LLM_PROVIDER",
        "DEEPSEEK_TIMEOUT_SECONDS",
        "DEEPSEEK_MAX_RETRIES",
        "PYTHON_DOTENV_DISABLED",
        "DATABASE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

    def load_test_env(*args):
        for name, value in dotenv_values(tmp_path / ".env").items():
            if value is not None and name not in os.environ:
                monkeypatch.setenv(name, value)

    monkeypatch.setattr(cli, "load_dotenv", load_test_env)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    client = Mock()
    client.recent.return_value = []
    monkeypatch.setattr(cli, "ChollometroClient", lambda: client)
    notifier = Mock()
    monkeypatch.setattr(cli, "TelegramNotifier", notifier)
    monkeypatch.setattr("sys.argv", ["chollo-alerts", "--db", ":memory:", "check"])
    return tmp_path, client, notifier


def test_project_root_is_repository_root():
    assert cli.PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_cli_loads_project_dotenv_from_another_working_directory(
    cli_env, monkeypatch, capsys
):
    root, client, notifier = cli_env
    (root / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=secret-token\nTELEGRAM_CHAT_ID=secret-chat\n"
        "LLM_ENABLED=true\nDEEPSEEK_API_KEY=secret-deepseek\n"
        "DEEPSEEK_MODEL=custom-model\n"
    )
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("TELEGRAM_BOT_TOKEN=wrong-token\n")
    monkeypatch.chdir(elsewhere)
    cli.main()
    notifier.assert_called_once_with("secret-token", "secret-chat")
    client.recent.assert_called_once()
    settings = config.LLMSettings.from_env()
    assert settings.enabled
    assert settings.api_key == "secret-deepseek"
    assert settings.model == "custom-model"
    output = capsys.readouterr()
    for secret in ("secret-token", "secret-chat", "secret-deepseek"):
        assert secret not in output.out + output.err
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DEEPSEEK_API_KEY",
        "LLM_ENABLED",
        "DEEPSEEK_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "token, chat_id, missing",
    [
        (None, None, ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")),
        ("secret-token", None, ("TELEGRAM_CHAT_ID",)),
        (None, "secret-chat", ("TELEGRAM_BOT_TOKEN",)),
        (" ", "", ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")),
    ],
)
def test_missing_telegram_credentials_are_clear_cli_error(
    cli_env, monkeypatch, capsys, token, chat_id, missing
):
    _, client, notifier = cli_env
    if token is not None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    if chat_id is not None:
        monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "Error de configuración" in output.err
    for name in missing:
        assert name in output.err
    assert "KeyError" not in output.err
    assert "Traceback" not in output.err
    assert "secret-token" not in output.err
    assert "secret-chat" not in output.err
    notifier.assert_not_called()
    client.recent.assert_not_called()


def test_dry_run_without_telegram_credentials(cli_env, monkeypatch, capsys):
    _, client, notifier = cli_env
    monkeypatch.setattr(
        "sys.argv", ["chollo-alerts", "--db", ":memory:", "check", "--dry-run"]
    )
    cli.main()
    notifier.assert_not_called()
    client.recent.assert_called_once()
    assert "TELEGRAM_SENT=0" in capsys.readouterr().out


def test_cli_uses_database_path_environment_default(cli_env, monkeypatch):
    root, _, _ = cli_env
    database = root / "data" / "chollometro.sqlite3"
    repository = Mock()
    repository.list_alert_rules.return_value = []
    monkeypatch.setenv("DATABASE_PATH", str(database))
    paths = []

    def make_repository(path):
        paths.append(path)
        return repository

    monkeypatch.setattr(cli, "DealRepository", make_repository)
    monkeypatch.setattr("sys.argv", ["chollo-alerts", "alert", "list"])

    cli.main()

    assert paths == [str(database)]
    assert repository.list_alert_rules.called


def test_existing_environment_credentials_take_precedence(cli_env, monkeypatch, capsys):
    root, client, notifier = cli_env
    (root / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=file-token\nTELEGRAM_CHAT_ID=file-chat\n"
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "environment-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "environment-chat")
    cli.main()
    notifier.assert_called_once_with("environment-token", "environment-chat")
    client.recent.assert_called_once()
    output = capsys.readouterr()
    assert "environment-token" not in output.out + output.err
    assert "environment-chat" not in output.out + output.err


def test_baseline_still_works_without_telegram_credentials(cli_env, monkeypatch):
    _, client, notifier = cli_env
    monkeypatch.setattr("sys.argv", ["chollo-alerts", "--db", ":memory:", "baseline"])
    cli.main()
    notifier.assert_not_called()
    client.recent.assert_called_once()


def test_telegram_poll_cli_wires_token_to_api_and_chat_id_to_authorization(
    cli_env, monkeypatch
):
    root, _, _ = cli_env
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_BOT_TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    monkeypatch.setattr(
        "sys.argv",
        ["chollo-alerts", "--db", str(root / "rules.sqlite3"), "telegram-poll"],
    )
    extractor = Mock()
    monkeypatch.setattr(cli, "DeepSeekProductExtractor", lambda: extractor)
    response = Mock()
    response.json.return_value = {"result": []}
    response.raise_for_status = Mock()
    get = Mock(return_value=response)
    monkeypatch.setattr("chollometro_alerts.telegram_rules.requests.get", get)
    monkeypatch.setattr(cli, "DealRepository", lambda _: Mock())
    seen = {}
    original_poll_once = cli.TelegramRuleController.poll_once

    def capture_controller(controller):
        seen["controller"] = controller
        return original_poll_once(controller)

    monkeypatch.setattr(cli.TelegramRuleController, "poll_once", capture_controller)

    cli.main()

    url = get.call_args.args[0]
    assert url == "https://api.telegram.org/bot123456:TEST_BOT_TOKEN/getUpdates"
    assert "987654321" not in url
    assert seen["controller"].authorized_chat_id == "987654321"
