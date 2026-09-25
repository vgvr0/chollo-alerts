from types import SimpleNamespace
from unittest.mock import Mock

from chollometro_alerts import cli


def make_settings(*, multiuser_enabled=False):
    return SimpleNamespace(
        bot_token="bot-token",
        authorized_chat_id="chat-id",
        multiuser_enabled=multiuser_enabled,
        auto_register=True,
        alert_nlp_mode="hybrid",
    )


def patch_runtime_dependencies(monkeypatch, tmp_path, telegram_settings):
    repository = Mock()
    service = object()
    client = Mock()
    feed = Mock()
    notifier = Mock()

    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli.TelegramSettings, "from_env", lambda: telegram_settings)
    monkeypatch.setattr(cli, "DealRepository", lambda path: repository)
    monkeypatch.setattr(cli, "ChollometroClient", client)
    monkeypatch.setattr(cli, "build_feed_client", feed)
    monkeypatch.setattr(cli, "TelegramNotifier", notifier)
    monkeypatch.setattr(cli, "AlertService", lambda *args, **kwargs: service)
    monkeypatch.setattr(
        "sys.argv", ["chollometro-alerts", "--db", str(tmp_path / "alerts.sqlite3")]
    )
    return repository, service, client, feed, notifier


def test_scan_cli_initializes_service_and_passes_it_to_scanner(monkeypatch, tmp_path):
    repository, service, client, feed, notifier = patch_runtime_dependencies(
        monkeypatch, tmp_path, make_settings(multiuser_enabled=True)
    )
    extractor = Mock()
    monkeypatch.setattr(cli, "create_extractor", extractor)
    scanner = Mock()
    monkeypatch.setattr(cli, "run_scanner", scanner)
    monkeypatch.setattr(
        "sys.argv",
        ["chollometro-alerts", "--db", str(tmp_path / "alerts.sqlite3"), "scan"],
    )

    cli.main()

    scanner.assert_called_once()
    assert scanner.call_args.args[0] is service
    extractor.assert_not_called()
    notifier.assert_called_once_with("bot-token", "chat-id", repository=repository)
    assert client.called
    assert feed.called


def test_run_cli_keeps_service_controller_and_multiuser_wiring(monkeypatch, tmp_path):
    repository, service, _, _, notifier = patch_runtime_dependencies(
        monkeypatch, tmp_path, make_settings(multiuser_enabled=True)
    )
    extractor = Mock()
    monkeypatch.setattr(cli, "create_extractor", extractor)
    controller = Mock()
    controller_type = Mock(return_value=controller)
    monkeypatch.setattr(cli, "TelegramRuleController", controller_type)
    daemon = Mock()
    monkeypatch.setattr(cli, "run_daemon", daemon)
    monkeypatch.setattr(
        "sys.argv",
        ["chollometro-alerts", "--db", str(tmp_path / "alerts.sqlite3"), "run"],
    )

    cli.main()

    daemon.assert_called_once()
    assert daemon.call_args.args[1] is service
    assert controller_type.call_args.kwargs["service"] is service
    extractor.assert_called_once_with()
    notifier.assert_called_once_with("bot-token", "chat-id", repository=repository)


def test_telegram_listen_does_not_initialize_scanner_components(monkeypatch, tmp_path):
    repository, _, client, feed, notifier = patch_runtime_dependencies(
        monkeypatch, tmp_path, make_settings()
    )
    extractor = Mock()
    monkeypatch.setattr(cli, "create_extractor", extractor)
    controller = Mock()
    monkeypatch.setattr(cli, "TelegramRuleController", Mock(return_value=controller))
    monkeypatch.setattr(
        "sys.argv",
        [
            "chollometro-alerts",
            "--db",
            str(tmp_path / "alerts.sqlite3"),
            "telegram-listen",
        ],
    )

    cli.main()

    controller.listen_forever.assert_called_once()
    extractor.assert_called_once_with()
    client.assert_not_called()
    feed.assert_not_called()
    notifier.assert_not_called()
    repository.close.assert_called_once_with()
