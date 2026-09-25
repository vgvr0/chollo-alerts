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
    client = Mock()
    feed = Mock()
    notifier = Mock()

    monkeypatch.setattr(cli, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(cli.TelegramSettings, "from_env", lambda: telegram_settings)
    monkeypatch.setattr(cli, "DealRepository", lambda path: repository)
    monkeypatch.setattr(cli, "ChollometroClient", client)
    monkeypatch.setattr(cli, "build_feed_client", feed)
    monkeypatch.setattr(cli, "TelegramNotifier", notifier)
    monkeypatch.setattr(
        "sys.argv", ["chollometro-alerts", "--db", str(tmp_path / "alerts.sqlite3")]
    )
    return repository, client, feed, notifier


def test_scan_cli_initializes_service_and_passes_it_to_scanner(monkeypatch, tmp_path):
    repository, client, feed, notifier = patch_runtime_dependencies(
        monkeypatch, tmp_path, make_settings(multiuser_enabled=True)
    )
    scanner = Mock()
    monkeypatch.setattr(cli, "run_scanner", scanner)
    monkeypatch.setattr(
        "sys.argv",
        [
            "chollometro-alerts",
            "--db",
            str(tmp_path / "alerts.sqlite3"),
            "--pages",
            "3",
            "scan",
            "--interval-minutes",
            "7",
        ],
    )

    cli.main()

    scanner.assert_called_once()
    service = scanner.call_args.args[0]
    assert isinstance(service, cli.AlertService)
    assert scanner.call_args.args[1:3] == (7, 3)
    notifier.assert_called_once_with("bot-token", "chat-id", repository=repository)
    assert client.called
    assert feed.called


def test_run_cli_keeps_service_controller_and_multiuser_wiring(monkeypatch, tmp_path):
    repository, _, _, notifier = patch_runtime_dependencies(
        monkeypatch, tmp_path, make_settings(multiuser_enabled=True)
    )
    extractor = Mock()
    monkeypatch.setattr(cli, "create_extractor", extractor)
    real_controller_type = cli.TelegramRuleController
    controller_instances = []

    def make_controller(**kwargs):
        controller = real_controller_type(**kwargs)
        controller_instances.append(controller)
        return controller

    controller_type = Mock(side_effect=make_controller)
    monkeypatch.setattr(cli, "TelegramRuleController", controller_type)
    daemon = Mock()
    monkeypatch.setattr(cli, "run_daemon", daemon)
    monkeypatch.setattr(
        "sys.argv",
        [
            "chollometro-alerts",
            "--db",
            str(tmp_path / "alerts.sqlite3"),
            "--pages",
            "2",
            "run",
            "--interval-minutes",
            "6",
        ],
    )

    cli.main()

    daemon.assert_called_once()
    service = daemon.call_args.args[1]
    assert isinstance(service, cli.AlertService)
    assert daemon.call_args.args[2:4] == (6, 2)
    assert controller_type.call_args.kwargs["service"] is service
    controller = controller_instances[0]
    assert isinstance(controller, real_controller_type)
    extractor.assert_called_once_with()
    notifier.assert_called_once_with("bot-token", "chat-id", repository=repository)
    assert controller_type.call_args.kwargs["repository"] is repository
    assert real_controller_type is not cli.TelegramRuleController


def test_telegram_listen_does_not_initialize_scanner_components(monkeypatch, tmp_path):
    repository, client, feed, notifier = patch_runtime_dependencies(
        monkeypatch, tmp_path, make_settings()
    )
    extractor = Mock()
    monkeypatch.setattr(cli, "create_extractor", extractor)
    real_controller_type = cli.TelegramRuleController
    listen_forever = Mock()
    monkeypatch.setattr(real_controller_type, "listen_forever", listen_forever)
    controller_instances = []

    def make_controller(**kwargs):
        controller = real_controller_type(**kwargs)
        controller_instances.append(controller)
        return controller

    monkeypatch.setattr(cli, "TelegramRuleController", make_controller)
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

    listen_forever.assert_called_once()
    controller = controller_instances[0]
    assert isinstance(controller, real_controller_type)
    extractor.assert_called_once_with()
    client.assert_not_called()
    feed.assert_not_called()
    notifier.assert_not_called()
    repository.close.assert_called_once_with()
