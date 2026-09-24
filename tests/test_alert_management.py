"""Routing the alert-management messages before the creation extractor.

The real case these tests pin: with "Avísame de cualquier chollo por menos de
200 euros" stored, "Elimina lo de cualquier aviso por menos de 200€" used to
reach the extractor and come back as "Falta el producto o la marca", although
it is a perfectly clear deletion. The operation is now decided before anything
is extracted (`intent_router`): a deletion only needs the alert it refers to,
an update rewrites that same alert, and only the sentences that really create
an alert keep the original interpretation path.
"""

import itertools
from decimal import Decimal

import pytest

from chollometro_alerts.alert_rule import AlertConstraints, AlertRule
from chollometro_alerts.intent import AlertIntent
from chollometro_alerts.intent_router import (
    classify_alert_operation,
    read_alert_reference,
)
from chollometro_alerts.repository import DealRepository
from chollometro_alerts.telegram_rules import TelegramRuleController

CHAT_ID = "42"

# Telegram update ids have to be unique per chat, and the repository refuses a
# message it has already seen: one counter for the whole module.
UPDATE_IDS = itertools.count(1)


class Translator:
    """Stands in for the provider, and records every message it is asked about.

    With no intent it fails loudly: a management message must never reach the
    creation extractor.
    """

    def __init__(self, intent=None):
        self.intent = intent
        self.seen = []

    def interpret_alert(self, text):
        self.seen.append(text)
        if self.intent is None:
            raise AssertionError("the creation extractor must not be called")
        return self.intent


class Controller(TelegramRuleController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.replies = []

    def send_message(self, text):
        self.replies.append(text)


def make_controller(tmp_path, translator=None):
    repository = DealRepository(tmp_path / "alerts.sqlite3")
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=repository,
        translator=translator or Translator(),
    )
    return controller, repository


def store(
    repository,
    *,
    query,
    product=None,
    brand=None,
    max_price=None,
    original=None,
    enabled=True,
):
    """Persist a structured alert exactly as a created one is stored."""
    return repository.save_alert_rule(
        AlertRule(
            query=query,
            product=product,
            brand=brand,
            constraints=AlertConstraints(
                max_price=Decimal(max_price) if max_price is not None else None
            ),
        ),
        original or query,
        enabled=enabled,
    )


def store_generic_alert(repository):
    """The alert of the reported case, with the sentence that created it."""
    return store(
        repository,
        query="cualquier chollo",
        max_price=200,
        original="Avísame de cualquier chollo por menos de 200 euros",
    )


def send(controller, text):
    return controller.process_update(
        {
            "update_id": next(UPDATE_IDS),
            "message": {"chat": {"id": CHAT_ID}, "text": text},
        }
    )


def create_intent(**overrides):
    fields = {
        "action": "create",
        "query": "cascos Sony",
        "product_type": "cascos",
        "brand": "Sony",
        "max_price": Decimal(200),
        "price_unit": "absolute",
    }
    fields.update(overrides)
    return AlertIntent(**fields)


# --- The operation, decided before anything is extracted -------------------- #


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("Avísame de portátiles Lenovo por menos de 700€", "CREATE_ALERT"),
        ("Elimina la alerta de Lenovo", "DELETE_ALERT"),
        ("Borra lo de portátiles por menos de 700€", "DELETE_ALERT"),
        ("Elimina lo de cualquier aviso por menos de 200€", "DELETE_ALERT"),
        ("Quita el límite de 200€ de la alerta de cascos", "UPDATE_ALERT"),
        ("Cambia el precio máximo de 200 a 150 euros", "UPDATE_ALERT"),
        ("Qué alertas tengo", "LIST_ALERTS"),
        ("Borra la de menos de 200€", "DELETE_ALERT"),
        ("Ya no quiero la alerta de menos de 200 euros", "DELETE_ALERT"),
        ("Deja de avisarme de los chollos de menos de 200€", "DELETE_ALERT"),
        ("Quita esa alerta", "DELETE_ALERT"),
        ("Cambia 200 por 150€", "UPDATE_ALERT"),
        ("Muéstrame mis alertas", "LIST_ALERTS"),
        # Nothing the router recognizes keeps the original creation path.
        ("alerta", "UNKNOWN"),
    ],
)
def test_the_operation_is_decided_before_any_extraction(text, operation):
    assert classify_alert_operation(text) == operation


@pytest.mark.parametrize(
    "text",
    [
        "Elimina lo de cualquier aviso por menos de 200€",
        "Quita esa alerta",
        "Qué alertas tengo",
    ],
)
def test_a_management_message_never_reaches_the_creation_extractor(tmp_path, text):
    translator = Translator()
    controller, repository = make_controller(tmp_path, translator)
    store_generic_alert(repository)

    send(controller, text)

    assert translator.seen == []


# --- Deleting an alert ------------------------------------------------------ #


def test_deleting_a_generic_alert_never_asks_for_a_product(tmp_path):
    """The reported case, end to end."""
    translator = Translator()
    controller, repository = make_controller(tmp_path, translator)
    store_generic_alert(repository)
    text = "Elimina lo de cualquier aviso por menos de 200€"

    reply = send(controller, text)

    assert classify_alert_operation(text) == "DELETE_ALERT"
    assert "aclaración" not in reply
    assert "Falta el producto o la marca" not in reply
    assert repository.list_alert_rules() == []
    assert "Alerta eliminada" in reply
    assert translator.seen == []


@pytest.mark.parametrize(
    "text",
    [
        "Borra la de menos de 200€",
        "Borra la de 200 euros",
        "Borra la de por debajo de 200",
        "Borra la de 200",
        "Ya no quiero la alerta de menos de 200 euros",
        "Deja de avisarme de los chollos de menos de 200€",
        "Borra lo de cascos por menos de 200€",
        "Quita la alerta de cascos",
        "Elimina la alerta de Sony",
    ],
)
def test_every_deletion_phrasing_deletes_the_alert_it_refers_to(tmp_path, text):
    controller, repository = make_controller(tmp_path)
    rule_id = store(
        repository,
        query="cascos Sony",
        product="cascos",
        brand="Sony",
        max_price=200,
        original="Avísame de cascos Sony por menos de 200€",
    )

    reply = send(controller, text)

    assert repository.list_alert_rules() == []
    assert f"#{rule_id}" in reply


def test_a_deletion_does_not_need_a_product_a_brand_or_a_category(tmp_path):
    """The extractor's requirements simply do not apply to a deletion."""
    controller, repository = make_controller(tmp_path)
    store_generic_alert(repository)

    reply = send(controller, "Borra la de menos de 200€")

    assert "Falta el producto" not in reply
    assert "categoría" not in reply
    assert repository.list_alert_rules() == []


def test_the_matching_is_not_exact_text_equality(tmp_path):
    """The stored text and the sentence are compared, never matched literally."""
    controller, repository = make_controller(tmp_path)
    store(
        repository,
        query="Portátiles Lenovo",
        product="portátiles",
        brand="Lenovo",
        max_price=700,
        original="Avísame de portátiles Lenovo por menos de 700 €",
    )

    reply = send(controller, "Borra lo de portatiles lenovo por menos de 700")

    assert repository.list_alert_rules() == []
    assert "Alerta eliminada" in reply


def test_several_plausible_alerts_are_never_deleted_arbitrarily(tmp_path):
    controller, repository = make_controller(tmp_path)
    first = store_generic_alert(repository)
    second = store(
        repository,
        query="zapatillas",
        product="zapatillas",
        max_price=200,
        original="Avísame de zapatillas por menos de 200€",
    )

    reply = send(controller, "Borra la de menos de 200€")

    assert len(repository.list_alert_rules()) == 2
    assert f"#{first}" in reply and f"#{second}" in reply
    assert "varias alertas" in reply

    # The question the bot asks is answerable: the second one is deleted.
    reply = send(controller, f"Elimina la alerta #{second}")

    assert [row[0] for row in repository.list_alert_rules()] == [first]
    assert f"#{second}" in reply


def test_an_alert_that_does_not_match_is_reported(tmp_path):
    controller, repository = make_controller(tmp_path)
    store_generic_alert(repository)

    reply = send(controller, "Borra la alerta de cascos Sony")

    assert len(repository.list_alert_rules()) == 1
    assert "No he encontrado" in reply


def test_a_shopped_alert_is_found_by_its_shop(tmp_path):
    """The shops of an alert are one more structured criterion of the match."""
    controller, repository = make_controller(tmp_path)
    rule_id = store_generic_alert(repository)
    kept = store(
        repository,
        query="portátiles Lenovo",
        product="portátiles",
        brand="Lenovo",
        max_price=700,
    )
    repository.attach_alert_rule(
        kept,
        AlertRule(
            query="portátiles Lenovo",
            product="portátiles",
            brand="Lenovo",
            include_merchants=("Amazon",),
            constraints=AlertConstraints(max_price=Decimal(700)),
        ),
    )

    reply = send(controller, "Borra la alerta de Amazon")

    assert [row[0] for row in repository.list_alert_rules()] == [rule_id]
    assert f"#{kept}" in reply


def test_a_provider_recognized_deletion_still_needs_no_product(tmp_path):
    """The provider may recognize a deletion this router does not name.

    Which alert disappears is decided by the sentence either way, so the
    extractor can never turn a deletion into a request for a product.
    """
    translator = Translator(AlertIntent(action="delete", query="chollo"))
    controller, repository = make_controller(tmp_path, translator)
    store_generic_alert(repository)
    text = "Déjame sin la de 200 euros"

    assert classify_alert_operation(text) == "UNKNOWN"
    reply = send(controller, text)

    assert translator.seen == [text]
    assert repository.list_alert_rules() == []
    assert "Falta el producto" not in reply


def test_an_update_can_change_the_price_dimension(tmp_path):
    controller, repository = make_controller(tmp_path)
    rule_id = store(
        repository,
        query="Coca-Cola",
        brand="Coca-Cola",
        max_price=0.5,
        original="Avísame de Coca-Cola por menos de 0,50 €",
    )

    reply = send(controller, "Cambia 0,50 a 0,60 € por unidad")

    constraints = repository.rule_by_id(rule_id).constraints
    assert constraints.max_price_per_unit == Decimal("0.60")
    assert constraints.max_price is None
    assert len(repository.list_alert_rules()) == 1
    assert "0,60" in reply


# --- Talking about "esa alerta" --------------------------------------------- #


def test_the_last_alert_shown_resolves_esa_alerta(tmp_path):
    controller, repository = make_controller(tmp_path)
    rule_id = store_generic_alert(repository)
    send(controller, "Qué alertas tengo")

    reply = send(controller, "Quita esa alerta")

    assert repository.list_alert_rules() == []
    assert f"#{rule_id}" in reply


def test_the_last_alert_created_resolves_esa_alerta(tmp_path):
    controller, repository = make_controller(tmp_path, Translator(create_intent()))

    assert "Alerta creada" in send(
        controller, "Avísame de cascos Sony por menos de 200€"
    )
    assert len(repository.list_alert_rules()) == 1

    reply = send(controller, "Quita esa alerta")

    assert repository.list_alert_rules() == []
    assert "Alerta eliminada" in reply


def test_esa_alerta_without_context_asks_which_one(tmp_path):
    controller, repository = make_controller(tmp_path)
    store_generic_alert(repository)

    reply = send(controller, "Quita esa alerta")

    assert len(repository.list_alert_rules()) == 1
    assert "No sé a qué alerta te refieres" in reply


# --- Updating an alert ------------------------------------------------------- #


def test_an_update_reuses_the_alert_and_only_changes_the_price(tmp_path):
    controller, repository = make_controller(tmp_path)
    rule_id = store(
        repository,
        query="cascos Sony",
        product="cascos",
        brand="Sony",
        max_price=200,
        original="Avísame de cascos Sony por menos de 200€",
    )

    reply = send(controller, "Cambia 200 por 150€")

    rows = repository.list_alert_rules()
    assert [row[0] for row in rows] == [rule_id]
    updated = repository.rule_by_id(rule_id)
    assert updated.constraints.max_price == Decimal(150)
    assert updated.product == "cascos" and updated.brand == "Sony"
    assert "Alerta actualizada" in reply and "150,00" in reply


def test_an_update_can_move_the_price_limit(tmp_path):
    controller, repository = make_controller(tmp_path)
    rule_id = store(
        repository,
        query="cascos Sony",
        product="cascos",
        brand="Sony",
        max_price=200,
        original="Avísame de cascos Sony por menos de 200€",
    )

    reply = send(controller, "Cambia el precio máximo de 200 a 150 euros")

    assert repository.rule_by_id(rule_id).constraints.max_price == Decimal(150)
    assert len(repository.list_alert_rules()) == 1
    assert "150,00" in reply


def test_an_update_can_remove_the_price_limit(tmp_path):
    controller, repository = make_controller(tmp_path)
    rule_id = store(
        repository,
        query="cascos Sony",
        product="cascos",
        brand="Sony",
        max_price=200,
        original="Avísame de cascos Sony por menos de 200€",
    )

    reply = send(controller, "Quita el límite de 200€ de la alerta de cascos")

    constraints = repository.rule_by_id(rule_id).constraints
    assert constraints.max_price is None
    assert constraints.max_price_per_unit is None
    assert constraints.max_price_per_liter is None
    assert len(repository.list_alert_rules()) == 1
    assert "Alerta actualizada" in reply


def test_an_update_without_a_target_is_reported(tmp_path):
    controller, repository = make_controller(tmp_path)
    store_generic_alert(repository)

    reply = send(controller, "Cambia el precio máximo de 900 a 150 euros")

    assert len(repository.list_alert_rules()) == 1
    assert "No he encontrado" in reply


def test_an_update_that_only_gives_the_new_value_uses_the_last_alert(tmp_path):
    """Nothing in "pon el precio en 150 €" tells the alerts apart: the last
    one the bot showed does."""
    controller, repository = make_controller(tmp_path)
    rule_id = store(
        repository,
        query="cascos Sony",
        product="cascos",
        brand="Sony",
        max_price=200,
    )
    send(controller, "Qué alertas tengo")

    reply = send(controller, "Pon el precio máximo en 150 €")

    assert repository.rule_by_id(rule_id).constraints.max_price == Decimal(150)
    assert "150,00" in reply


def test_a_provider_recognized_update_without_a_product_uses_the_reference(tmp_path):
    """An update that names no alert can only be about a stored one."""
    translator = Translator(
        AlertIntent(action="update", max_price=Decimal(150), price_unit="absolute")
    )
    controller, repository = make_controller(tmp_path, translator)
    rule_id = store(
        repository,
        query="cascos Sony",
        product="cascos",
        brand="Sony",
        max_price=200,
    )
    send(controller, "Qué alertas tengo")
    text = "Ponme un tope de 150 €"

    assert classify_alert_operation(text) == "UNKNOWN"
    reply = send(controller, text)

    assert translator.seen == [text]
    assert repository.rule_by_id(rule_id).constraints.max_price == Decimal(150)
    assert "150,00" in reply


# --- The creation path keeps working ----------------------------------------- #


def test_a_creation_sentence_still_goes_through_the_extractor(tmp_path):
    translator = Translator(create_intent())
    controller, repository = make_controller(tmp_path, translator)

    reply = send(controller, "Avísame de cascos Sony por menos de 200€")

    assert translator.seen == ["Avísame de cascos Sony por menos de 200€"]
    rows = repository.list_alert_rules()
    assert len(rows) == 1 and rows[0][1] == "cascos Sony"
    assert "Alerta creada" in reply


def test_listing_shows_every_stored_alert(tmp_path):
    controller, repository = make_controller(tmp_path)
    store_generic_alert(repository)
    store(repository, query="zapatillas", product="zapatillas", max_price=80)

    reply = send(controller, "Qué alertas tengo")

    assert "Tus alertas" in reply
    assert "cualquier chollo" in reply.lower() and "zapatillas" in reply.lower()


# --- Reading the reference --------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "Borra la de 200€",
        "Borra la de 200 euros",
        "Borra la de menos de 200",
        "Borra la de por menos de 200",
        "Borra la de por debajo de 200",
        "Borra la de 200,00",
    ],
)
def test_the_price_of_a_reference_is_read_in_every_spelling(text):
    assert read_alert_reference(text).identity_price == Decimal(200)


@pytest.mark.parametrize(
    "text",
    [
        "borra lo de cualquier aviso por menos de 200€",
        "elimina lo de cualquier aviso por menos de 200€",
        "quita lo de cualquier aviso por menos de 200€",
        "ya no quiero el aviso de menos de 200€",
        "deja de avisarme de los chollos de menos de 200€",
    ],
)
def test_every_deletion_phrasing_is_a_deletion(text):
    assert classify_alert_operation(text) == "DELETE_ALERT"


@pytest.mark.parametrize(
    "text",
    [
        "No quiero chollos por debajo de 100 grados",
        "Ya no quiero chollos por debajo de 100 grados",
        "No quiero más chollos por debajo de 250°",
    ],
)
def test_a_temperature_condition_is_never_read_as_a_deletion(text):
    """Degrees describe the deals the operator wants, not one to remove."""
    assert classify_alert_operation(text) == "UNKNOWN"


def test_a_temperature_sentence_keeps_being_a_creation(tmp_path):
    """The negative case of the delete cues, end to end."""
    translator = Translator(
        AlertIntent(
            action="create",
            query="chollos",
            max_price=Decimal(200),
            price_unit="absolute",
            temperature_min=20,
        )
    )
    controller, repository = make_controller(tmp_path, translator)
    store_generic_alert(repository)
    text = "Ya no quiero que me avises por debajo de 20 grados"

    assert classify_alert_operation(text) == "CREATE_ALERT"
    reply = send(controller, text)

    assert translator.seen == [text]
    assert "Alerta creada" in reply
    assert len(repository.list_alert_rules()) == 2


# --- The three points of the diff review ------------------------------------- #


def test_a_paused_alert_can_still_be_deleted(tmp_path):
    """A paused alert is still the operator's alert."""
    controller, repository = make_controller(tmp_path)
    rule_id = store(
        repository,
        query="cascos Sony",
        product="cascos",
        brand="Sony",
        max_price=200,
        enabled=False,
    )

    reply = send(controller, "Elimina la alerta de cascos")

    assert repository.list_alert_rules() == []
    assert f"#{rule_id}" in reply


def test_the_context_table_is_created_on_a_database_that_lacks_it(tmp_path):
    """`alert_context` is additive: an older SQLite file still opens and works."""
    path = tmp_path / "alerts.sqlite3"
    old = DealRepository(path)
    rule_id = store_generic_alert(old)
    old.db.execute("DROP TABLE alert_context")
    old.db.commit()
    old.close()

    reopened = DealRepository(path)
    controller = Controller(
        bot_token="token",
        authorized_chat_id=CHAT_ID,
        repository=reopened,
        translator=Translator(),
    )
    assert f"#{rule_id}" in send(controller, "Qué alertas tengo")

    reply = send(controller, "Quita esa alerta")

    assert reopened.list_alert_rules() == []
    assert f"#{rule_id}" in reply


def test_a_context_pointing_at_a_deleted_alert_does_not_break(tmp_path):
    """The remembered ids may outlive the alerts they point at."""
    controller, repository = make_controller(tmp_path)
    store_generic_alert(repository)
    repository.set_alert_context(CHAT_ID, [999])

    reply = send(controller, "Quita esa alerta")

    assert len(repository.list_alert_rules()) == 1
    assert "No sé a qué alerta te refieres" in reply


def test_the_whole_management_flow_the_operator_walks_through(tmp_path):
    """One test per step of the manual checklist, in order.

    create → list → delete by the price → two similar alerts (the ambiguous
    order deletes nothing) → answer with the number → create and remove the
    last one → change the last one, which keeps a single row.
    """
    translator = Translator()
    controller, repository = make_controller(tmp_path, translator)

    def create(text, **overrides):
        """A created alert, activated the way the baseline leaves it."""
        fields = {"action": "create", "query": "chollos", "price_unit": "absolute"}
        fields.update(overrides)
        translator.intent = AlertIntent(**fields)
        reply = send(controller, text)
        translator.intent = None
        rule_id = repository.list_alert_rules()[-1][0]
        repository.set_rule_state(rule_id, "ACTIVE", enabled=True)
        return rule_id, reply

    def ids():
        return [row[0] for row in repository.list_alert_rules()]

    first, reply = create("Avísame de chollos por menos de 200€", max_price=200)
    assert "Alerta creada" in reply and ids() == [first]

    assert f"#{first}" in send(controller, "Lista mis alertas")

    assert f"#{first}" in send(controller, "Elimina lo de menos de 200€")
    assert ids() == []

    second, _ = create("Avísame de chollos por menos de 200€", max_price=200)
    third, _ = create(
        "Avísame de ofertas por menos de 200€", query="ofertas", max_price=200
    )
    reply = send(controller, "Elimina lo de menos de 200€")
    assert ids() == [second, third]
    assert f"#{second}" in reply and f"#{third}" in reply

    assert f"#{second}" in send(controller, f"Elimina la alerta #{second}")
    assert ids() == [third]

    fourth, _ = create(
        "Avísame de zapatillas por menos de 300€",
        query="zapatillas",
        product_type="zapatillas",
        max_price=300,
    )
    assert f"#{fourth}" in send(controller, "Quita esa alerta")
    assert ids() == [third]

    fifth, _ = create(
        "Avísame de cascos Sony por menos de 200€",
        query="cascos Sony",
        product_type="cascos",
        brand="Sony",
        max_price=200,
    )
    reply = send(controller, "Cambia esa alerta a menos de 150€")

    assert "Alerta actualizada" in reply and "150,00" in reply
    assert ids() == [third, fifth]
    assert repository.rule_by_id(fifth).constraints.max_price == Decimal(150)
