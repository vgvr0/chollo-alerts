"""The Telegram alert says which alert matched and why, with real evidence.

A notification must name the alert that produced the match (never the deal
category) and quote the conditions the engine really compared. Nothing is
inferred: a fact the rule could not prove produces no line, and a `generic`
deal is never a wildcard.
"""

from datetime import timedelta
from decimal import Decimal

import test_graphql_discovery as discovery

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.evaluation import (
    MatchEvidence,
    interest_rule_from_alert,
)
from chollometro_alerts.filters import apply_rule
from chollometro_alerts.models import Deal
from chollometro_alerts.product import ProductExtraction
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.service import AlertService
from chollometro_alerts.telegram import format_message


class Client:
    """The HTML provider, scripted: it always returns the same deals."""

    def __init__(self, deals):
        self.deals = list(deals)

    def recent(self, queries, pages):
        return list(self.deals)


class RecordingNotifier:
    dry_run = False

    def __init__(self, failing_sends=0):
        self.sent = []
        self.evidences = []
        self.failing_sends = failing_sends

    def send(self, deal, evidence=None):
        if self.failing_sends > 0:
            self.failing_sends -= 1
            raise RuntimeError("telegram down")
        self.sent.append(deal)
        self.evidences.append(evidence)

    @property
    def messages(self):
        """Exactly what Telegram would have received, in order."""
        return [
            format_message(deal, evidence)
            for deal, evidence in zip(self.sent, self.evidences, strict=True)
        ]


def no_provider_call(product_text, deal_id=None):
    raise AssertionError("a cached extraction must never reach the provider")


def despertador(price="7.95", **overrides):
    fields = {
        "deal_id": "despertador-1",
        "title": "Luz despertador GRUNDIG que simula un amanecer + alarma y Snooze",
        "url": "https://www.chollometro.com/ofertas/luz-despertador-grundig",
        "price": Decimal(price) if price is not None else None,
        "merchant": "Action",
        "temperature": 90,
        "category": "generic",
        "published_at": None,
    }
    fields.update(overrides)
    return Deal(**fields, product_text=fields["title"])


def store_rule(
    repository,
    *,
    query,
    product=None,
    brand=None,
    constraints=None,
    original_text=None,
):
    """Persist one alert exactly like the Telegram flow does."""
    return repository.save_alert_rule(
        AlertRule(
            query=query,
            product=product,
            brand=brand,
            constraints=constraints or AlertConstraints(),
        ),
        original_text or query,
    )


def cache_extraction(repository, deal_id, extraction):
    repository.save_extraction(deal_id, extraction.model_dump(mode="json"))


def make_service(tmp_path, deals, notifier=None):
    repository = DealRepository(tmp_path / "evidence.db")
    notifier = notifier if notifier is not None else RecordingNotifier()
    service = AlertService(Client(deals), repository, notifier, no_provider_call)
    return service, repository, notifier


def conditions(deal, extraction, rule):
    """The evidence the engine produces for one deal, without any side effect."""
    priced = Deal(**{**deal.__dict__, "product_extraction": extraction})
    return apply_rule(priced, interest_rule_from_alert(rule)).checks


def test_a_deterministic_match_names_the_alert_and_the_conditions_it_checked():
    deal = despertador()
    extraction = ProductExtraction(
        product_type="despertador", extraction_source="deterministic"
    )
    rule = AlertRule(
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
    )
    evidence = MatchEvidence(
        rule_id=7,
        alert_text="Despertadores por menos de 15 €",
        query="despertador",
        method="deterministic",
        checks=conditions(deal, extraction, rule),
    )

    assert format_message(deal, evidence) == (
        "🔔 Chollo encontrado\n"
        "\n"
        f"{deal.title}\n"
        "\n"
        "💰 Precio: 7,95 €\n"
        "🏪 Tienda: Action\n"
        "🔥 Temperatura: 90°\n"
        "\n"
        "🎯 Alerta:\n"
        '"Despertadores por menos de 15 €"\n'
        "\n"
        "✅ Cumple:\n"
        "• Producto buscado: «despertador» (detectado: «despertador»)\n"
        "• Precio máximo: 7,95 € ≤ 15 €\n"
        "\n"
        "🧠 Evaluación: deterministic\n"
        "\n"
        f"{deal.url}"
    )


def test_a_bare_message_still_names_the_deal_without_inventing_anything():
    deal = despertador()
    message = format_message(deal)
    assert message.startswith("🔔 Chollo encontrado\n\n" + deal.title)
    assert "🎯 Alerta:" not in message
    assert "Cumple" not in message
    assert "generic" not in message


def test_the_production_scan_sends_that_same_explanation(tmp_path):
    deal = despertador()
    service, repository, notifier = make_service(tmp_path, [deal])
    rule_id = store_rule(
        repository,
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
        original_text="Despertadores por menos de 15 €",
    )
    cache_extraction(
        repository,
        deal.deal_id,
        ProductExtraction(
            product_type="despertador", extraction_source="deterministic"
        ),
    )

    assert service.run_active_rules() == 1

    (evidence,) = notifier.evidences
    message = notifier.messages[0]
    assert (evidence.rule_id, evidence.alert_text, evidence.query) == (
        rule_id,
        "Despertadores por menos de 15 €",
        "despertador",
    )
    assert evidence.method == "deterministic"
    assert [check.code for check in evidence.checks] == ["PRODUCT", "MAX_PRICE"]
    assert '"Despertadores por menos de 15 €"' in message
    assert "• Producto buscado: «despertador» (detectado: «despertador»)" in message
    assert "• Precio máximo: 7,95 € ≤ 15 €" in message
    assert "🧠 Evaluación: deterministic" in message
    assert "generic" not in message


def test_a_price_only_match_shows_only_the_condition_it_evaluated(tmp_path):
    deal = despertador(title="Cafetera de goteo 1,5 L")
    service, repository, notifier = make_service(tmp_path, [deal])
    store_rule(
        repository,
        query="cafetera",
        constraints=AlertConstraints(max_price=Decimal(15)),
    )
    cache_extraction(
        repository, deal.deal_id, ProductExtraction(extraction_source="deterministic")
    )

    assert service.run_active_rules() == 1

    message = notifier.messages[0]
    assert "• Precio máximo: 7,95 € ≤ 15 €" in message
    assert "Producto buscado" not in message
    assert "Marca" not in message
    assert [check.code for check in notifier.evidences[0].checks] == ["MAX_PRICE"]


def test_an_llm_match_reports_the_model_supplied_fact_as_its_reason(tmp_path):
    deal = despertador()
    service, repository, notifier = make_service(tmp_path, [deal])
    store_rule(
        repository,
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
        original_text="Despertadores por menos de 15 €",
    )
    # Only the model can call this a "despertador": the local parser cannot.
    cache_extraction(
        repository,
        deal.deal_id,
        ProductExtraction(product_type="despertador", extraction_source="llm"),
    )

    assert service.run_active_rules() == 1

    message = notifier.messages[0]
    assert "🧠 Evaluación: llm" in message
    assert "🤖 Coincidencia semántica:" in message
    assert "Hechos aportados por el modelo: producto «despertador»." in message
    # Only a brief verdict: no prompt, no chain of thought, no tokens.
    assert "prompt" not in message.lower()
    assert "token" not in message.lower()


def test_a_hybrid_match_mixes_deterministic_checks_and_model_facts(tmp_path):
    deal = despertador(
        title="Leche Pascual 6 x 1 L",
        price="5.94",
        merchant="Carrefour",
        temperature=5,
    )
    service, repository, notifier = make_service(tmp_path, [deal])
    store_rule(
        repository,
        query="leche",
        product="leche",
        brand="Pascual",
        constraints=AlertConstraints(max_price_per_liter=Decimal("1.10")),
        original_text="Leche Pascual por menos de 1,10 € por litro",
    )
    # The deterministic parser supplies the volume; only the model knows the brand.
    cache_extraction(
        repository,
        deal.deal_id,
        ProductExtraction(
            product_type="leche",
            brand="Pascual",
            units=6,
            unit_volume_l=Decimal(1),
            total_volume_l=Decimal(6),
            extraction_source="hybrid",
        ),
    )

    assert service.run_active_rules() == 1

    message = notifier.messages[0]
    assert "🧠 Evaluación: hybrid" in message
    assert "• Marca: «Pascual» (detectada: «Pascual»)" in message
    assert "• Precio por litro: 0,99 €/L ≤ 1,10 €/L" in message
    assert "🤖 Coincidencia semántica:" in message
    assert "Hechos aportados por el modelo: marca «Pascual»." in message


def test_a_generic_deal_that_does_not_match_the_alert_is_never_notified(tmp_path):
    deal = despertador(title="Cerveza Mahou 24 latas", price="12.99")
    assert deal.category == "generic"
    service, repository, notifier = make_service(tmp_path, [deal])
    rule_id = store_rule(
        repository,
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
        original_text="Despertadores por menos de 15 €",
    )
    # A product that really is a beer is not a "despertador", and being
    # `generic` does not turn the alert into a wildcard.
    cache_extraction(
        repository,
        deal.deal_id,
        ProductExtraction(product_type="cerveza", extraction_source="deterministic"),
    )

    assert service.run_active_rules() == 0

    assert notifier.sent == [] and notifier.evidences == []
    observation = repository.get_rule_observation(rule_id, deal.deal_id)
    assert (observation[5], observation[6]) == (0, "REJECTED_PRODUCT")
    assert repository.rule_observation_evidence(rule_id, deal.deal_id) is None


def test_the_notification_names_the_alert_that_actually_matched(tmp_path):
    deal = despertador()
    service, repository, notifier = make_service(tmp_path, [deal])
    winner = store_rule(
        repository,
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
        original_text="Despertadores por menos de 15 €",
    )
    store_rule(
        repository,
        query="zapatillas",
        product="zapatillas",
        constraints=AlertConstraints(max_price=Decimal(50)),
        original_text="Zapatillas por menos de 50 €",
    )
    cache_extraction(
        repository,
        deal.deal_id,
        ProductExtraction(
            product_type="despertador", extraction_source="deterministic"
        ),
    )

    assert service.run_active_rules() == 1

    (evidence,) = notifier.evidences
    assert evidence.rule_id == winner
    assert evidence.alert_text == "Despertadores por menos de 15 €"
    assert "Zapatillas" not in notifier.messages[0]


def test_two_alerts_for_one_deal_keep_independent_reasons(tmp_path):
    deal = despertador()
    service, repository, notifier = make_service(tmp_path, [deal])
    cheap = store_rule(
        repository,
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
        original_text="Despertadores por menos de 15 €",
    )
    cheaper = store_rule(
        repository,
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(8)),
        original_text="Despertadores por menos de 8 €",
    )
    cache_extraction(
        repository,
        deal.deal_id,
        ProductExtraction(
            product_type="despertador", extraction_source="deterministic"
        ),
    )

    assert service.run_active_rules() == 2

    by_rule = {
        evidence.rule_id: (evidence, message)
        for evidence, message in zip(notifier.evidences, notifier.messages, strict=True)
    }
    assert set(by_rule) == {cheap, cheaper}
    assert by_rule[cheap][0].alert_text == "Despertadores por menos de 15 €"
    assert "• Precio máximo: 7,95 € ≤ 15 €" in by_rule[cheap][1]
    assert '"Despertadores por menos de 15 €"' in by_rule[cheap][1]
    assert by_rule[cheaper][0].alert_text == "Despertadores por menos de 8 €"
    assert "• Precio máximo: 7,95 € ≤ 8 €" in by_rule[cheaper][1]
    assert '"Despertadores por menos de 8 €"' in by_rule[cheaper][1]
    assert "≤ 15 €" not in by_rule[cheaper][1]
    assert len({sent.deal_id for sent in notifier.sent}) == 1


def test_a_condition_that_was_never_evaluated_is_never_claimed(tmp_path):
    deal = despertador(price=None)
    service, repository, notifier = make_service(tmp_path, [deal])
    store_rule(
        repository,
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
        original_text="Despertadores por menos de 15 €",
    )
    cache_extraction(
        repository,
        deal.deal_id,
        ProductExtraction(
            product_type="despertador", extraction_source="deterministic"
        ),
    )

    assert service.run_active_rules() == 1

    message = notifier.messages[0]
    assert "💰 Precio: N/D" in message
    # The rule's price ceiling was not compared with anything, so it is silent.
    assert "Precio máximo" not in message
    assert "≤ 15 €" not in message
    assert [check.code for check in notifier.evidences[0].checks] == ["PRODUCT"]


def test_an_unknown_fact_rejects_instead_of_being_reported_as_met(tmp_path):
    deal = despertador(title="Pack Coca-Cola 24 latas")
    service, repository, notifier = make_service(tmp_path, [deal])
    rule_id = store_rule(
        repository,
        query="coca-cola",
        product="refresco",
        constraints=AlertConstraints(max_price_per_unit=Decimal("0.50")),
        original_text="Coca-Cola por menos de 0,50 € por unidad",
    )
    # The model knows the product but not the quantity: the unit price cannot
    # be checked, so the deal is rejected instead of being announced.
    cache_extraction(
        repository,
        deal.deal_id,
        ProductExtraction(product_type="refresco", extraction_source="llm"),
    )

    assert service.run_active_rules() == 0

    assert notifier.sent == []
    observation = repository.get_rule_observation(rule_id, deal.deal_id)
    assert observation[6] == "REJECTED_UNKNOWN_QUANTITY"
    assert repository.rule_observation_evidence(rule_id, deal.deal_id) is None


def test_a_retried_notification_keeps_the_original_explanation(tmp_path):
    """A Telegram failure is retried without losing the original explanation."""
    notifier = discovery.RecordingNotifier(failing_sends=1)
    # A predates the alert: the first cycle only records it as seen.
    baseline = [discovery.make_deal("A", discovery.BEFORE)]
    deal = discovery.make_deal(
        "D",
        discovery.AFTER + timedelta(minutes=1),
        price="7.95",
        title="Luz despertador GRUNDIG que simula un amanecer + alarma",
    )
    feed = discovery.FakeFeed(list(baseline), [deal, *baseline], list(baseline))
    service, repository, _notifier, _extractor = discovery.make_service(
        tmp_path,
        feed,
        notifier=notifier,
        extractor=lambda product_text, deal_id=None: {"product_type": "despertador"},
    )
    rule_id = discovery.add_rule(
        repository,
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
    )

    service.run_active_rules()  # bootstrap: the current feed is only a reference
    # The delivery fails after the match is already durable.
    assert service.run_active_rules() == 0
    assert notifier.sent == []
    assert repository.pending_rule_notifications() == [(rule_id, deal.deal_id)]
    stored = repository.rule_observation_evidence(rule_id, deal.deal_id)
    assert stored["method"] == "llm"

    # The next cycle has no new threads: the pending alert is still delivered.
    assert service.run_active_rules() == 1

    assert notifier.evidences[0] == MatchEvidence.from_dict(stored)
    assert notifier.messages[0] == format_message(deal, notifier.evidences[0])
    assert '"despertador"' in notifier.messages[0]
    assert "• Precio máximo: 7,95 € ≤ 15 €" in notifier.messages[0]
    assert (
        "Hechos aportados por el modelo: producto «despertador»."
        in (notifier.messages[0])
    )
    assert "🧠 Evaluación: llm" in notifier.messages[0]
    assert repository.pending_rule_notifications() == []


def test_the_stored_evidence_survives_the_repository_round_trip(tmp_path):
    repository = DealRepository(tmp_path / "roundtrip.db")
    deal = despertador()
    rule = AlertRule(
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
    )
    extraction = ProductExtraction(
        product_type="despertador", extraction_source="hybrid"
    )
    evidence = MatchEvidence(
        rule_id=1,
        alert_text="Despertadores por menos de 15 €",
        query="despertador",
        method="hybrid",
        checks=conditions(deal, extraction, rule),
        semantic_reason="Hechos aportados por el modelo: producto «despertador».",
    )
    store_rule(
        repository,
        query="despertador",
        product="despertador",
        constraints=AlertConstraints(max_price=Decimal(15)),
    )
    repository.claim_rule_observation(1, deal.deal_id)
    repository.record_rule_observation_result(
        1, deal.deal_id, True, None, evidence=evidence.as_dict()
    )

    stored = repository.rule_observation_evidence(1, deal.deal_id)
    assert stored == evidence.as_dict()
    assert MatchEvidence.from_dict(stored) == evidence


def test_a_verdict_without_stored_evidence_is_still_renderable():
    """Rows written before the evidence existed must not break the retry."""
    assert MatchEvidence.from_dict(None) is None
    assert MatchEvidence.from_dict("not a payload") is None
    assert MatchEvidence.from_dict({"checks": [{"label": "x"}]}).checks == ()
