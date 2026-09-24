from decimal import Decimal

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.intent import AlertIntent
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.telegram_rules import TelegramRuleController


class NoopTranslator:
    pass


def controller(repository):
    return TelegramRuleController(
        bot_token="token",
        authorized_chat_id="42",
        repository=repository,
        translator=NoopTranslator(),
    )


def save(repository, **fields):
    enabled = fields.pop("enabled", True)
    return repository.save_alert_rule(
        AlertRule(**fields),
        fields["query"],
        enabled=enabled,
    )


def listing(repository):
    rows = repository.list_alert_rules()
    return controller(repository)._format(AlertIntent(action="list"), rows)


def test_listing_uses_structured_identity_and_icons(tmp_path):
    repository = DealRepository(tmp_path / "alerts.sqlite3")
    save(
        repository,
        query="Lagavulin pr",
        brand="Lagavulin",
        constraints=AlertConstraints(max_price=Decimal(50)),
    )
    save(
        repository,
        query="zapatillas",
        product="zapatilla",
        brand="ASICS",
        constraints=AlertConstraints(max_price=Decimal(200)),
        enabled=False,
    )

    text = listing(repository)

    assert "✅ #1 · Lagavulin" in text
    assert "Lagavulin pr" not in text
    assert "⏸️ #2 · Zapatillas ASICS" in text


def test_listing_formats_absolute_and_per_liter_prices(tmp_path):
    repository = DealRepository(tmp_path / "alerts.sqlite3")
    save(
        repository,
        query="móviles",
        constraints=AlertConstraints(max_price=Decimal(1000)),
    )
    save(
        repository,
        query="leche",
        constraints=AlertConstraints(max_price_per_liter=Decimal("0.79")),
    )

    text = listing(repository)

    assert "💶 Menos de 1.000,00 €" in text
    assert "💶 Menos de 0,79 €/L" in text


def test_listing_shows_optional_filters_and_summary(tmp_path):
    repository = DealRepository(tmp_path / "alerts.sqlite3")
    save(
        repository,
        query="cascos",
        constraints=AlertConstraints(
            max_price=Decimal(20), temperature_min=100, temperature_max=300
        ),
        include_merchants=("Amazon", "PcComponentes"),
        exclude_merchants=("AliExpress",),
    )
    save(
        repository,
        query="relojes",
        constraints=AlertConstraints(max_price=Decimal(30)),
        enabled=False,
    )

    text = listing(repository)

    assert "🏪 Amazon, PcComponentes" in text
    assert "🚫 AliExpress" in text
    assert "🔥 Temperatura: 100°–300°" in text
    assert "1 alerta activa · 1 inactiva" in text
    assert "None" not in text
