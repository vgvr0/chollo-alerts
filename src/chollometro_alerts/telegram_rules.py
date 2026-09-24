import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal

import requests

from .alert_text import merge_intent
from .errors import ChollometroError
from .intent import AlertIntent, intent_to_rule, notification_window, validate_intent
from .intent_router import (
    alert_candidates,
    classify_alert_operation,
    read_alert_reference,
    resolve_alert_reference,
    updated_rule,
)
from .models import format_number
from .telegram import post_with_retry

logger = logging.getLogger(__name__)

# How the persisted price dimension is rendered back to the user. An absolute
# price is the total price of the deal, so it has no "/unit" suffix at all.
PRICE_UNIT_LABELS = {"liter": "L", "unit": "ud", "kilogram": "kg"}


def _spanish_amount(value) -> str:
    """Render a rule amount with two decimals and Spanish thousands groups."""
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    integer, decimals = f"{amount:.2f}".split(".")
    groups = []
    while integer:
        groups.append(integer[-3:])
        integer = integer[:-3]
    return ".".join(reversed(groups)) + "," + decimals


def _display_words(value: str) -> str:
    """Make stored identity fields readable without damaging acronyms."""
    words = []
    for word in value.split():
        if word.isupper() or (
            word.isalpha() and len(word) <= 2 and word.lower() in {"pc", "tv"}
        ):
            words.append(word.upper())
        else:
            words.append(word[:1].upper() + word[1:].lower())
    return " ".join(words)


def alert_display_name(rule, fallback: str | None = None) -> str:
    """Build the human-facing identity from the structured rule first."""
    query = (rule.query or fallback or "alerta").strip()
    product = (rule.product or "").strip()
    brand = (rule.brand or "").strip()
    if brand and not product:
        return _display_words(brand)
    if not product and not brand:
        return query
    if brand:
        query_without_brand = re.sub(re.escape(brand), "", query, flags=re.IGNORECASE)
        query_without_brand = " ".join(query_without_brand.split())
    else:
        query_without_brand = query
    product_name = query_without_brand or product
    if product_name and brand:
        return _display_words(f"{product_name} {brand}")
    return _display_words(product_name or brand or query)


def listing_price_line(constraints) -> str | None:
    """Render the stored price restriction for the visual alert listing."""
    for value, unit in (
        (constraints.max_price, "absolute"),
        (constraints.max_price_per_unit, "unit"),
        (constraints.max_price_per_liter, "liter"),
        (getattr(constraints, "max_price_per_kilogram", None), "kilogram"),
    ):
        if value is not None:
            return f"💶 Menos de {_spanish_amount(value)} {price_suffix(unit)}"
    return None


def listing_temperature_line(constraints) -> str | None:
    minimum = constraints.temperature_min
    maximum = constraints.temperature_max
    if minimum is not None and maximum is not None:
        return f"🔥 Temperatura: {degrees(minimum)}–{degrees(maximum)}"
    if minimum is not None:
        return f"🔥 Temperatura mínima: {degrees(minimum)}"
    if maximum is not None:
        return f"🔥 Temperatura máxima: {degrees(maximum)}"
    return None


def price_suffix(price_unit):
    label = PRICE_UNIT_LABELS.get(price_unit or "absolute")
    return f"€/{label}" if label else "€"


def degrees(value) -> str:
    """One Chollometro temperature as it is written back to the operator."""
    return f"{format_number(value)}°"


def temperature_condition(minimum, maximum) -> str | None:
    """How the temperature window of an alert reads: None when it has none."""
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None:
        return f"entre {degrees(minimum)} y {degrees(maximum)}"
    if minimum is not None:
        return f"al menos {degrees(minimum)}"
    return f"como máximo {degrees(maximum)}"


def rule_price_text(rule) -> str:
    """The price condition a stored alert carries, as it reads back.

    An alert may also have no price at all (its condition is a temperature),
    which is stated instead of invented.
    """
    constraints = rule.constraints
    for value, unit in (
        (constraints.max_price, "absolute"),
        (constraints.max_price_per_unit, "unit"),
        (constraints.max_price_per_liter, "liter"),
    ):
        if value is not None:
            amount = f"{float(value):.2f}".replace(".", ",")
            return f"< {amount} {price_suffix(unit)}"
    return "sin precio"


def alert_detail_lines(intent) -> list[str]:
    """The shops and the schedule a created alert really stores.

    Both are shown back to the operator so the confirmation says what the alert
    will do, and the schedule line states explicitly that a deal found outside
    it is not lost.
    """
    lines = []
    temperature = temperature_condition(intent.temperature_min, intent.temperature_max)
    if temperature:
        lines.append(f"🌡️ Temperatura: {temperature}")
    if intent.include_merchants or intent.exclude_merchants:
        shops = ", ".join(intent.include_merchants or ("cualquier tienda",))
        if intent.exclude_merchants:
            shops += f" (excepto {', '.join(intent.exclude_merchants)})"
        lines.append(f"🏪 Tiendas: {shops}")
    window = notification_window(intent)
    if window is not None:
        stamp = f"{window.start:%H:%M}–{window.end:%H:%M} {window.timezone}"
        lines.append(
            f"⏱️ Avisos: {stamp}. Los chollos que aparezcan fuera "
            "de ese horario no se pierden: quedan pendientes y se envían al "
            "abrirse la ventana."
        )
    return lines


class TelegramRuleController:
    def __init__(
        self,
        *,
        bot_token,
        authorized_chat_id,
        repository,
        translator,
        timeout=20,
        retries=2,
        backoff=0.5,
        sleep=time.sleep,
        service=None,
    ):
        self.url = f"https://api.telegram.org/bot{bot_token}"
        self.authorized_chat_id = str(authorized_chat_id)
        self.repository = repository
        self.translator = translator
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self.service = service

    def process_update(self, update):
        update_id = update.get("update_id")
        message = update.get("message") or {}
        if (
            update_id is None
            or str(message.get("chat", {}).get("id")) != self.authorized_chat_id
        ):
            return None
        if not self.repository.claim_telegram_update(update_id):
            return None
        text = message.get("text", "").strip()
        try:
            try:
                reply = self._reply_to(text)
            except ValueError as exc:
                reply = f"Necesito una aclaración: {exc}"
            except ChollometroError as exc:
                # Never answer "alerta creada, 0 ofertas" when Chollometro failed.
                reply = (
                    f"⚠️ No he podido consultar Chollometro ahora mismo "
                    f"({exc.error_type}). La alerta no se ha activado y no se ha "
                    "guardado ninguna referencia. Vuelve a enviar el mensaje para "
                    "reintentarlo."
                )
                logger.warning("alert_baseline_failed error_type=%s", exc.error_type)
            self.send_message(reply)
        except Exception:
            release = getattr(self.repository, "release_telegram_update", None)
            if release is not None:
                release(update_id)
            raise
        return reply

    def _reply_to(self, text):
        """Answer one message: manage the stored alerts, or create a new one.

        The operation is decided *before* anything is extracted. A deletion is
        never sent to the extractor (it does not need a product, a brand or a
        category: it needs the alert it refers to), and an update reuses the
        stored alert instead of building a second one from the sentence.
        """
        operation = classify_alert_operation(text)
        if operation == "LIST_ALERTS":
            return self._handle_list_alerts()
        if operation == "DELETE_ALERT":
            return self._handle_delete_alert(text)
        if operation == "UPDATE_ALERT":
            return self._handle_update_alert(text)
        # CREATE_ALERT, and anything this router does not recognize, keep the
        # original path: the sentence is interpreted and merged as before.
        return self._handle_create_or_unknown(text)

    def _handle_create_or_unknown(self, text):
        """The creation path, unchanged: interpret the sentence, then store it."""
        if self.translator is None:
            raise ValueError(
                "la creación de alertas desde Telegram requiere LLM_ENABLED=true "
                "y DEEPSEEK_API_KEY"
            )
        # The merchant lists and the notification window are read from the
        # sentence itself before anything is persisted: they must never be
        # an invention of the model, and a vague period ("por la noche")
        # asks for the exact hours instead of guessing them.
        intent = merge_intent(self.translator.interpret_alert(text), text)
        if intent.action == "delete":
            # The provider recognized a deletion this router did not: which
            # alert disappears is still decided by the sentence, never by the
            # product a deletion does not have to mention.
            return self._handle_delete_alert(text)
        if intent.action == "update" and not (
            intent.query or intent.product_type or intent.brand
        ):
            # An update that names no alert can only be about a stored one.
            return self._handle_update_alert(text)
        intent = validate_intent(intent)
        rows = self.repository.apply_alert_intent(intent)
        if intent.action in {"create", "update"}:
            target = next(
                (
                    row
                    for row in rows
                    if row[1] == (intent.query or intent.product_type or intent.brand)
                ),
                None,
            )
            if target:
                self.repository.attach_alert_rule(
                    target[0], intent_to_rule(intent), text
                )
                # From now on, "esa alerta" means the one just created.
                self._remember_alerts([target[0]])
        baseline_count = None
        if intent.action == "create" and self.service is not None:
            query = intent.query or intent.product_type or intent.brand
            rule = next((r for r in rows if r[1] == query), None)
            # A rule whose baseline could not be taken stays disabled, and
            # retrying the same message must be allowed to complete it.
            if rule is not None and self.repository.get_rule(rule[0])[7] in {
                "INITIALIZING",
                "INITIALIZING_FAILED",
            }:
                baseline_count = self.service.baseline_rule(rule[0], query)
                rows = self.repository.list_alert_rules()
        if intent.action == "list":
            self._remember_alerts([row[0] for row in rows])
        elif intent.action == "delete":
            self._clear_remembered()
        return self._format(intent, rows, baseline_count)

    def _handle_list_alerts(self):
        """Show every stored alert and remember them as the last ones shown."""
        rows = self.repository.list_alert_rules() if self.repository is not None else []
        self._remember_alerts([row[0] for row in rows])
        return self._format(AlertIntent(action="list"), rows)

    def _handle_delete_alert(self, text):
        """Delete the alert the sentence refers to, without extracting anything."""
        reference = read_alert_reference(text, "DELETE_ALERT")
        resolution = self._resolve(reference)
        if resolution.status == "unique":
            candidate = resolution.match
            self.repository.delete_alert_rule(candidate.rule_id)
            self._clear_remembered()
            return f"🗑️ Alerta eliminada: {self._alert_label(candidate)}"
        if resolution.status == "ambiguous":
            self._remember_alerts([item.rule_id for item in resolution.matches])
            return self._ambiguous_reply("eliminar", resolution.matches)
        return self._missing_alert_reply(resolution.status)

    def _handle_update_alert(self, text):
        """Change only the properties the sentence asks for, on the stored alert."""
        reference = read_alert_reference(text, "UPDATE_ALERT")
        if not reference.has_change:
            return (
                "🤔 Dime qué quieres cambiar de esa alerta, por ejemplo "
                "«cambia 200 a 150 €» o «quita el límite de 200 €»."
            )
        resolution = self._resolve(reference)
        if resolution.status == "unique":
            candidate = resolution.match
            rule = updated_rule(candidate.rule, reference, legacy=candidate.legacy)
            try:
                self.repository.replace_alert_rule(candidate.rule_id, rule, text)
            except ValueError as exc:
                return f"⚠️ No he podido actualizarla: {exc}."
            self._remember_alerts([candidate.rule_id])
            return self._format(self._intent_from_rule(rule, "update"), [])
        if resolution.status == "ambiguous":
            self._remember_alerts([item.rule_id for item in resolution.matches])
            return self._ambiguous_reply("actualizar", resolution.matches)
        return self._missing_alert_reply(resolution.status)

    def _resolve(self, reference):
        return resolve_alert_reference(
            reference, self._alert_candidates(), self._remembered()
        )

    def _alert_candidates(self):
        return alert_candidates(self.repository) if self.repository is not None else ()

    @staticmethod
    def _alert_label(candidate):
        return (
            f"#{candidate.rule_id} — {candidate.rule.query} — "
            f"{rule_price_text(candidate.rule)}"
        )

    @staticmethod
    def _ambiguous_reply(verb, candidates):
        listed = "\n".join(
            f"#{item.rule_id} — {item.rule.query} — {rule_price_text(item.rule)}"
            for item in candidates
        )
        example = f"«Elimina la alerta #{candidates[0].rule_id}»"
        return (
            f"🔎 He encontrado varias alertas que podrían ser esa. "
            f"¿Cuál quieres {verb}?\n\n{listed}\n\n"
            f"Respóndeme con su número (por ejemplo {example}) o con más detalle."
        )

    @staticmethod
    def _missing_alert_reply(status):
        if status == "context_required":
            return (
                "🤔 No sé a qué alerta te refieres. Dime su número (#1) o "
                "descríbela; con «qué alertas tengo» te las enseño."
            )
        return (
            "🤔 No he encontrado ninguna alerta que coincida con esa "
            "descripción. Con «qué alertas tengo» te enseño las que tienes."
        )

    @staticmethod
    def _intent_from_rule(rule, action):
        """The intent of a stored alert, so its reply reads like a creation one."""
        constraints = rule.constraints
        fields = {
            "action": action,
            "query": rule.query,
            "product_type": rule.product,
            "brand": rule.brand,
            "include_merchants": list(rule.include_merchants) or None,
            "exclude_merchants": list(rule.exclude_merchants) or None,
        }
        for unit, name in (
            ("absolute", "max_price"),
            ("unit", "max_price_per_unit"),
            ("liter", "max_price_per_liter"),
        ):
            value = getattr(constraints, name, None)
            if value is not None:
                fields["max_price"] = value
                fields["price_unit"] = unit
                break
        window = rule.notification_window
        if window is not None:
            fields["notify_window_start"] = f"{window.start:%H:%M}"
            fields["notify_window_end"] = f"{window.end:%H:%M}"
            fields["notify_timezone"] = window.timezone
        # Every other condition the intent model knows about is carried over,
        # so the reply describes what the update really left stored.
        for name in ("temperature_min", "temperature_max"):
            if name in AlertIntent.model_fields:
                fields[name] = getattr(constraints, name, None)
        return AlertIntent(**fields)

    def _remember_alerts(self, rule_ids):
        """Remember the alert(s) the bot just created, changed or showed."""
        if self.repository is None:
            return
        self.repository.set_alert_context(self.authorized_chat_id, rule_ids)

    def _remembered(self):
        if self.repository is None:
            return ()
        return self.repository.get_alert_context(self.authorized_chat_id)

    def _clear_remembered(self):
        if self.repository is None:
            return
        self.repository.clear_alert_context(self.authorized_chat_id)

    def poll_once(self, offset=None):
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        response = requests.get(
            f"{self.url}/getUpdates", params=params, timeout=self.timeout
        )
        response.raise_for_status()
        for update in response.json().get("result", []):
            self.process_update(update)

    def listen_forever(self, stop_event=None, poll_timeout=45, max_backoff=60):
        """Consume Telegram updates until stopped, tolerating transient failures."""
        offset = None
        backoff = 1
        while stop_event is None or not stop_event.is_set():
            try:
                params = {"timeout": poll_timeout}
                if offset is not None:
                    params["offset"] = offset
                response = requests.get(
                    f"{self.url}/getUpdates", params=params, timeout=poll_timeout + 10
                )
                response.raise_for_status()
                self.repository.runtime_telegram_activity()
                updates = response.json().get("result", [])
                for update in updates:
                    update_id = update.get("update_id")
                    logger.info("telegram_update_received update_id=%s", update_id)
                    try:
                        self.process_update(update)
                        if update_id is not None:
                            offset = max(offset or update_id, update_id + 1)
                    except Exception:
                        logger.exception(
                            "telegram_update_failed update_id=%s", update_id
                        )
                backoff = 1
            except (requests.RequestException, ValueError):
                self.repository.runtime_telegram_failure()
                logger.warning("telegram.poll.failed retry_in_seconds=%s", backoff)
                if stop_event is not None:
                    stop_event.wait(backoff)
                else:
                    time.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)

    def send_message(self, text):
        post_with_retry(
            f"{self.url}/sendMessage",
            json={"chat_id": self.authorized_chat_id, "text": text},
            timeout=self.timeout,
            retries=self.retries,
            backoff=self.backoff,
            sleep=self._sleep,
        )

    def _format(self, intent, rows, baseline_count=None):
        if intent.action == "list":
            if not rows:
                return "🔔 Tus alertas\n\nNo tienes alertas configuradas."
            blocks = [self._listing_line(row) for row in rows]
            active = sum(bool(row[6]) for row in rows)
            inactive = len(rows) - active
            return (
                "🔔 Tus alertas\n\n"
                + "\n\n".join(blocks)
                + "\n\n"
                + self._listing_summary(active, inactive)
            )
        verb = {
            "create": "Alerta creada",
            "update": "Alerta actualizada",
            "delete": "Alerta eliminada",
            "enable": "Alerta activada",
            "disable": "Alerta desactivada",
        }[intent.action]
        subject = intent.query or intent.product_type or intent.brand or "alerta"
        if intent.action in {"create", "update"}:
            conditions = []
            if intent.max_price is not None:
                limit = f"{intent.max_price:.2f}".replace(".", ",")
                conditions.append(
                    f"por debajo de {limit} {price_suffix(intent.price_unit)}"
                )
            temperature = temperature_condition(
                intent.temperature_min, intent.temperature_max
            )
            if temperature:
                # With a price, "… y al menos 250°" completes the sentence; on
                # its own, it needs the preposition the price condition gave it.
                conditions.append(temperature if conditions else f"con {temperature}")
            reply = f"✅ {verb}: {subject}"
            if conditions:
                reply += " " + " y ".join(conditions)
            details = alert_detail_lines(intent)
            if details:
                reply += "\n" + "\n".join(details)
            if baseline_count is not None and intent.action == "create":
                reply += f"\n🔎 {baseline_count} ofertas actuales guardadas como referencia.\nTe avisaré de las nuevas que cumplan la condición."
            return reply
        icon = {"delete": "🗑️", "disable": "⏸️", "enable": "▶️"}[intent.action]
        return f"{icon} {verb}: {subject}"

    def _listing_line(self, row):
        """One stored alert as Telegram lists it, read through its own rule.

        The canonical rule is what says which condition the alert really has:
        the legacy `max_price` column cannot tell an alert without a price from
        a price of zero, and a temperature-only alert has no price at all.
        """
        rule = self._stored_rule(row)
        if rule is None:
            return (
                f"{'✅' if row[6] else '⏸️'} #{row[0]} · {_display_words(row[1])}\n"
                f"💶 {self._legacy_price(row)}"
            )
        lines = [
            f"{'✅' if row[6] else '⏸️'} #{row[0]} · {alert_display_name(rule, row[1])}"
        ]
        price = listing_price_line(rule.constraints)
        if price:
            lines.append(price)
        temperature = listing_temperature_line(rule.constraints)
        if temperature:
            lines.append(temperature)
        if rule.include_merchants:
            lines.append(f"🏪 {', '.join(rule.include_merchants)}")
        if rule.exclude_merchants:
            lines.append(f"🚫 {', '.join(rule.exclude_merchants)}")
        return "\n".join(lines)

    @staticmethod
    def _listing_summary(active, inactive):
        active_word = "alerta activa" if active == 1 else "alertas activas"
        inactive_word = "inactiva" if inactive == 1 else "inactivas"
        return f"{active} {active_word} · {inactive} {inactive_word}"

    def _stored_rule(self, row):
        if self.repository is None:
            return None
        try:
            return self.repository.rule_from_listing(row)
        except (ValueError, TypeError):
            # A row the canonical boundary cannot resolve is still listed, with
            # the legacy columns it does have.
            return None

    @staticmethod
    def _legacy_price(row):
        """The legacy price column of a row, or "sin precio" when it has none."""
        if row[4] in (None, "", "None"):
            return "sin precio"
        return f"Menos de {_spanish_amount(row[4])} {price_suffix(row[5])}"
