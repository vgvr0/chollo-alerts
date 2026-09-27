import logging
import re
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import requests

from .alert_text import deterministic_price_alert, merge_intent
from .errors import ChollometroError
from .i18n import DEFAULT_LANGUAGE, LANGUAGE_NAMES, Translator
from .intent import AlertIntent, intent_to_rule, notification_window, validate_intent
from .intent_router import (
    alert_candidates,
    classify_alert_operation,
    read_alert_reference,
    recent_deal_limit,
    resolve_alert_reference,
    updated_rule,
)
from .models import Deal, format_amount, format_number
from .schedule import as_aware_utc, default_timezone
from .telegram import post_with_retry
from .telegram_users import TelegramUserResolver

logger = logging.getLogger(__name__)

# How the persisted price dimension is rendered back to the user. An absolute
# price is the total price of the deal, so it has no "/unit" suffix at all.
PRICE_UNIT_LABELS = {"liter": "L", "unit": "ud", "kilogram": "kg"}
_DEFAULT_I18N = Translator()


def _t(key, language=DEFAULT_LANGUAGE, **values):
    return _DEFAULT_I18N.t(key, locale=language, **values)


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
        elif "-" in word:
            words.append(
                "-".join(
                    part[:1].upper() + part[1:].lower() for part in word.split("-")
                )
            )
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
        return _display_words(query)
    if brand:
        query_without_brand = re.sub(re.escape(brand), "", query, flags=re.IGNORECASE)
        query_without_brand = " ".join(query_without_brand.split())
    else:
        query_without_brand = query
    product_name = query_without_brand or product
    if product_name and brand:
        return _display_words(f"{product_name} {brand}")
    return _display_words(product_name or brand or query)


def listing_price_line(constraints, language=DEFAULT_LANGUAGE) -> str | None:
    """Render the stored price restriction for the visual alert listing."""
    for value, unit in (
        (constraints.max_price, "absolute"),
        (constraints.max_price_per_unit, "unit"),
        (constraints.max_price_per_liter, "liter"),
        (getattr(constraints, "max_price_per_kilogram", None), "kilogram"),
    ):
        if value is not None:
            return _t(
                "rules.price_under",
                language,
                amount=_spanish_amount(value),
                currency=price_suffix(unit),
            )
    return None


def listing_temperature_line(constraints, language=DEFAULT_LANGUAGE) -> str | None:
    minimum = constraints.temperature_min
    maximum = constraints.temperature_max
    if minimum is not None and maximum is not None:
        return _t(
            "rules.temperature",
            language,
            value=f"{degrees(minimum)}–{degrees(maximum)}",
        )
    if minimum is not None:
        return _t("rules.temperature_min", language, value=degrees(minimum))
    if maximum is not None:
        return _t("rules.temperature_max", language, value=degrees(maximum))
    return None


def legacy_listing_price(row, language=DEFAULT_LANGUAGE) -> str:
    """Render the legacy price column when canonical resolution is unavailable."""
    if row[4] in (None, "", "None"):
        return _t("rules.no_price", language)
    return _t(
        "rules.price_under",
        language,
        amount=_spanish_amount(row[4]),
        currency=price_suffix(row[5]),
    )


def listing_identity(row, rule) -> str:
    """Render the stable id and human-readable name used by the listing."""
    if rule is None:
        return f"#{row[0]} · {_display_words(row[1])}"
    # A partially repaired structured row may still have useful identity in
    # the legacy columns. Use it for display only; never write it back.
    display_rule = (
        rule.model_copy(update={"brand": row[3]}) if not rule.brand and row[3] else rule
    )
    return f"#{row[0]} · {alert_display_name(display_rule, row[1])}"


def format_listing_line(row, rule, language=DEFAULT_LANGUAGE) -> str:
    """Render one alert for every read-only listing surface."""
    lines = [f"{'✅' if row[6] else '⏸️'} {listing_identity(row, rule)}"]
    if rule is None:
        lines.append(f"💶 {legacy_listing_price(row, language)}")
        return "\n".join(lines)
    # A partially repaired structured row may still have useful identity in
    # the legacy columns. Use it for display only; never write it back.
    display_rule = (
        rule.model_copy(update={"brand": row[3]}) if not rule.brand and row[3] else rule
    )
    price = listing_price_line(display_rule.constraints, language)
    if price:
        lines.append(price)
    temperature = listing_temperature_line(display_rule.constraints, language)
    if temperature:
        lines.append(temperature)
    if display_rule.constraints.category_include:
        lines.append(
            _t(
                "rules.category",
                language,
                value=", ".join(display_rule.constraints.category_include),
            )
        )
    if display_rule.constraints.category_exclude:
        lines.append(
            _t(
                "rules.category_excluded",
                language,
                value=", ".join(display_rule.constraints.category_exclude),
            )
        )
    if display_rule.constraints.max_age_minutes is not None:
        lines.append(
            _t(
                "rules.age",
                language,
                value=format_number(display_rule.constraints.max_age_minutes),
            )
        )
    if not price and not temperature:
        lines.append(_t("rules.any_new", language))
    if display_rule.include_merchants:
        lines.append(
            _t(
                "rules.merchants",
                language,
                value=", ".join(display_rule.include_merchants),
            )
        )
    if display_rule.exclude_merchants:
        lines.append(
            _t(
                "rules.excluded",
                language,
                value=", ".join(display_rule.exclude_merchants),
            )
        )
    return "\n".join(lines)


def listing_summary(active, inactive, language=DEFAULT_LANGUAGE) -> str:
    active_word = _t("rules.active_summary", language, count=active)
    inactive_word = _t("rules.inactive_summary", language, count=inactive)
    return f"{active_word} · {inactive} {inactive_word}"


def format_alert_list(rows, rule_loader, language=DEFAULT_LANGUAGE) -> str:
    """Render the canonical alert list for Telegram and the CLI."""
    if not rows:
        return _t("rules.none", language)
    resolved_rows = []
    for row in rows:
        try:
            rule = rule_loader(row)
        except (ValueError, TypeError):
            rule = None
        resolved_rows.append((row, rule))
    blocks = [format_listing_line(row, rule, language) for row, rule in resolved_rows]
    active = sum(bool(row[6]) for row in rows)
    inactive = len(rows) - active
    result = (
        _t("rules.header", language)
        + "\n\n"
        + "\n\n".join(blocks)
        + "\n\n"
        + listing_summary(active, inactive, language)
    )
    if inactive:
        label = _t("rules.inactive_label", language, count=inactive)
        inactive_names = [
            listing_identity(row, rule) for row, rule in resolved_rows if not row[6]
        ]
        result += "\n" + _t(
            "rules.inactive", language, label=label, value=", ".join(inactive_names)
        )
    return result


def price_suffix(price_unit):
    label = PRICE_UNIT_LABELS.get(price_unit or "absolute")
    return f"€/{label}" if label else "€"


def degrees(value) -> str:
    """One Chollometro temperature as it is written back to the operator."""
    return f"{format_number(value)}°"


def temperature_condition(minimum, maximum, language=DEFAULT_LANGUAGE) -> str | None:
    """How the temperature window of an alert reads: None when it has none."""
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None:
        return _t(
            "rules.temperature_condition_between",
            language,
            minimum=degrees(minimum),
            maximum=degrees(maximum),
        )
    if minimum is not None:
        return _t("rules.temperature_condition_min", language, minimum=degrees(minimum))
    return _t("rules.temperature_condition_max", language, maximum=degrees(maximum))


def rule_price_text(rule, language=DEFAULT_LANGUAGE) -> str:
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
    return _t("rules.no_price", language)


def alert_detail_lines(intent, language=DEFAULT_LANGUAGE) -> list[str]:
    """The shops and the schedule a created alert really stores.

    Both are shown back to the operator so the confirmation says what the alert
    will do, and the schedule line states explicitly that a deal found outside
    it is not lost.
    """
    lines = []
    temperature = temperature_condition(
        intent.temperature_min, intent.temperature_max, language
    )
    if temperature:
        lines.append(_t("rules.detail_temperature", language, value=temperature))
    if getattr(intent, "category_include", None):
        lines.append(
            _t(
                "rules.detail_categories",
                language,
                value=", ".join(intent.category_include),
            )
        )
    if getattr(intent, "category_exclude", None):
        lines.append(
            _t(
                "rules.detail_excluded_categories",
                language,
                value=", ".join(intent.category_exclude),
            )
        )
    if getattr(intent, "max_age_minutes", None) is not None:
        lines.append(
            _t(
                "rules.detail_age",
                language,
                value=format_number(intent.max_age_minutes),
            )
        )
    if intent.include_merchants or intent.exclude_merchants:
        shops = ", ".join(
            intent.include_merchants or (_t("rules.any_store", language),)
        )
        if intent.exclude_merchants:
            shops += (
                " ("
                + _t(
                    "rules.except", language, value=", ".join(intent.exclude_merchants)
                )
                + ")"
            )
        lines.append(_t("rules.detail_merchants", language, value=shops))
    window = notification_window(intent)
    if window is not None:
        stamp = f"{window.start:%H:%M}–{window.end:%H:%M} {window.timezone}"
        lines.append(_t("rules.detail_schedule", language, stamp=stamp))
    return lines


def _recent_deal_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    try:
        local = as_aware_utc(value).astimezone(ZoneInfo(default_timezone()))
    except (TypeError, ValueError):
        return None
    return f"{local:%d/%m/%Y %H:%M}"


def format_recent_deals(
    deals: list[Deal] | tuple[Deal, ...], language=DEFAULT_LANGUAGE
) -> str:
    """Render persisted deals compactly for a Telegram read-only response."""
    if not deals:
        return _t("deals.none", language)
    lines = [_t("deals.latest_header", language), ""]
    for index, deal in enumerate(deals, start=1):
        details = [deal.title]
        if deal.price is not None:
            details.append(format_amount(deal.price))
        if deal.temperature is not None:
            details.append(f"{deal.temperature}°")
        if deal.merchant:
            details.append(deal.merchant)
        lines.append(f"{index}. " + " — ".join(details))
        published = _recent_deal_datetime(deal.published_at)
        if published is not None:
            lines.append(_t("deals.published_time", language, value=published))
        if deal.url:
            lines.append(deal.url)
        if index != len(deals):
            lines.append("")
    return "\n".join(lines)


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
        multiuser_enabled=False,
        auto_register=False,
        alert_nlp_mode="hybrid",
        i18n=None,
    ):
        self.url = f"https://api.telegram.org/bot{bot_token}"
        self.authorized_chat_id = str(authorized_chat_id)
        self.repository = repository
        self.translator = translator
        self.i18n = i18n or Translator()
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep
        self.service = service
        self.multiuser_enabled = multiuser_enabled
        if alert_nlp_mode not in {"deterministic", "hybrid", "llm_first"}:
            raise ValueError(
                "alert_nlp_mode debe ser deterministic, hybrid o llm_first"
            )
        self.alert_nlp_mode = alert_nlp_mode
        self.last_interpretation_method = None
        self.last_llm_success = None
        self.current_user_id = None
        self.current_language_user_id = None
        self.current_language = DEFAULT_LANGUAGE
        self.current_chat_id = self.authorized_chat_id
        self.user_resolver = TelegramUserResolver(
            repository,
            enabled=multiuser_enabled,
            auto_register=auto_register,
            legacy_chat_id=authorized_chat_id,
        )

    def process_update(self, update):
        update_id = update.get("update_id")
        message = update.get("message") or {}
        if update_id is None:
            return None
        chat = message.get("chat") or {}
        chat_type = chat.get("type")
        if chat_type is not None and chat_type != "private":
            self.current_chat_id = str(chat.get("id", self.authorized_chat_id))
            if self.multiuser_enabled:
                self.send_message(self._t("error.private_chat_only"))
            return None
        if self.multiuser_enabled and not TelegramUserResolver.is_allowed_chat(update):
            return None
        if (
            not self.multiuser_enabled
            and str(message.get("chat", {}).get("id")) != self.authorized_chat_id
        ):
            return None
        user = self.user_resolver.resolve(update)
        if user is None:
            if not self.multiuser_enabled:
                return None
            self.current_chat_id = str(
                message.get("chat", {}).get("id", self.authorized_chat_id)
            )
            self.send_message(self._t("error.access_denied"))
            return None
        self.current_user_id = user.id if self.multiuser_enabled else None
        self.current_language_user_id = user.id
        self.current_language = user.language
        self.current_chat_id = str(
            message.get("chat", {}).get("id", self.authorized_chat_id)
        )
        if not self.repository.claim_telegram_update(update_id):
            return None
        text = message.get("text", "").strip()
        try:
            try:
                reply = self._reply_to(text)
            except ValueError as exc:
                error = (
                    self._t("errors.empty_alert")
                    if str(exc) == "errors.empty_alert"
                    else str(exc)
                )
                reply = self._t("errors.clarification", error=error)
            except ChollometroError as exc:
                # Never answer "alerta creada, 0 ofertas" when Chollometro failed.
                reply = self._t("errors.provider", error_type=exc.error_type)
                logger.warning("alert_baseline_failed error_type=%s", exc.error_type)
            self.send_message(reply)
        except Exception:
            release = getattr(self.repository, "release_telegram_update", None)
            if release is not None:
                release(update_id)
            raise
        return reply

    def _t(self, key, **values):
        return self.i18n.t(key, locale=self.current_language, **values)

    def _handle_language_command(self, text):
        match = re.fullmatch(r"/language(?:@\w+)?(?:\s+([\w-]+))?", text, re.IGNORECASE)
        if not match:
            return None
        requested = match.group(1)
        if requested is None:
            return self._t(
                "language.current",
                language=LANGUAGE_NAMES.get(
                    self.current_language, self.current_language
                ),
            )
        language = self.i18n.normalize_language(requested)
        if not self.i18n.is_supported(language):
            return self._t("language.unsupported", requested=requested)
        user_id = self.current_language_user_id
        if user_id is None:
            return self._t("language.usage")
        self.repository.update_user_language(user_id, language)
        self.current_language = language
        return self._t("language.changed", language=LANGUAGE_NAMES[language])

    def _reply_to(self, text):
        """Answer one message: manage the stored alerts, or create a new one.

        The operation is decided *before* anything is extracted. A deletion is
        never sent to the extractor (it does not need a product, a brand or a
        category: it needs the alert it refers to), and an update reuses the
        stored alert instead of building a second one from the sentence.
        """
        language_reply = self._handle_language_command(text)
        if language_reply is not None:
            return language_reply
        operation = classify_alert_operation(text)
        if operation == "CAPABILITY_QUESTION":
            return self._t("help.capability")
        if operation == "RECENT_DEALS":
            return self._handle_recent_deals(text)
        if operation == "LIST_ALERTS":
            return self._handle_list_alerts()
        if operation == "DELETE_ALERT":
            return self._handle_delete_alert(text)
        if operation == "UPDATE_ALERT":
            return self._handle_update_alert(text)
        # CREATE_ALERT, and anything this router does not recognize, keep the
        # original path: the sentence is interpreted and merged as before.
        return self._handle_create_or_unknown(text)

    def _handle_recent_deals(self, text):
        """Read recent deals through the service, never through the router."""
        limit = recent_deal_limit(text)
        service = self.service
        if service is None:
            # Telegram poll/listen modes intentionally do not attach the scan
            # service because alert creation must keep its old no-baseline
            # behaviour. A read-only service instance still preserves the
            # Telegram -> service -> repository boundary for this operation.
            from .service import AlertService

            service = AlertService(None, self.repository, None)
        return format_recent_deals(service.recent_deals(limit), self.current_language)

    def _handle_create_or_unknown(self, text):
        """Interpret and persist a creation without changing the rule engine."""
        intent = self._interpret_alert(text)
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
        existing_ids = {
            row[0]
            for row in self.repository.list_alert_rules(user_id=self.current_user_id)
        }
        rows = self.repository.apply_alert_intent(intent, user_id=self.current_user_id)
        if intent.action in {"create", "update"}:
            target = next(
                (row for row in rows if row[0] not in existing_ids),
                None,
            )
            if target is None:
                target = next(
                    (
                        row
                        for row in rows
                        if row[1]
                        == (intent.query or intent.product_type or intent.brand)
                        or (
                            not row[1]
                            and not (
                                intent.query or intent.product_type or intent.brand
                            )
                        )
                    ),
                    None,
                )
            if target:
                self.repository.attach_alert_rule(
                    target[0],
                    intent_to_rule(intent),
                    text,
                    user_id=self.current_user_id,
                )
                # From now on, "esa alerta" means the one just created.
                self._remember_alerts([target[0]])
        baseline_count = None
        if intent.action == "create" and self.service is not None:
            query = intent.query or intent.product_type or intent.brand
            rule = next((r for r in rows if r[1] == query), None)
            # A rule whose baseline could not be taken stays disabled, and
            # retrying the same message must be allowed to complete it.
            if rule is not None and self.repository.get_rule(
                rule[0], user_id=self.current_user_id
            )[7] in {
                "INITIALIZING",
                "INITIALIZING_FAILED",
            }:
                baseline_count = self.service.baseline_rule(rule[0], query)
                rows = self.repository.list_alert_rules(user_id=self.current_user_id)
        elif intent.action == "create" and self.service is None:
            # Poll/listen mode may not have a scanner attached.  The rule is
            # still a valid active alert; a later scan can evaluate it.  Keep
            # INITIALIZING only for the service-backed baseline transaction,
            # where existing deals must be excluded before activation.
            query = intent.query or intent.product_type or intent.brand
            rule = next((r for r in rows if r[1] == query), None)
            if rule is not None:
                self.repository.set_rule_state(
                    rule[0], "ACTIVE", enabled=True, user_id=self.current_user_id
                )
                rows = self.repository.list_alert_rules(user_id=self.current_user_id)
        if intent.action == "list":
            self._remember_alerts([row[0] for row in rows])
        elif intent.action == "delete":
            self._clear_remembered()
        return self._format(intent, rows, baseline_count)

    def _interpret_alert(self, text):
        """Interpret an alert with validation and a safe deterministic fallback."""
        deterministic = deterministic_price_alert(text)
        if (
            self.alert_nlp_mode in {"deterministic", "hybrid"}
            and deterministic is not None
        ):
            self._record_interpretation("deterministic", llm_success=False)
            return validate_intent(deterministic)
        if self.alert_nlp_mode == "deterministic":
            self._record_interpretation("deterministic", llm_success=False)
            raise ValueError(self._t("errors.parser"))
        if self.translator is not None:
            try:
                candidate = merge_intent(self.translator.interpret_alert(text), text)
                # Management intents are structurally validated by Pydantic,
                # but their product/reference requirements belong to the
                # existing delete/update resolver below.
                if candidate.action in {
                    "delete",
                    "update",
                    "enable",
                    "disable",
                    "list",
                }:
                    self._record_interpretation("llm", llm_success=True)
                    return candidate
                validated = validate_intent(candidate)
                self._record_interpretation("llm", llm_success=True)
                return validated
            except Exception as exc:
                # A vague notification period is a domain clarification, not
                # a provider failure; preserve the existing conversation flow.
                if isinstance(exc, ValueError) and str(exc).startswith("«"):
                    raise
                logger.warning(
                    "alert_interpretation nlp_mode=%s interpretation_method=fallback "
                    "llm_success=false llm_failure=true error_type=%s",
                    self.alert_nlp_mode,
                    type(exc).__name__,
                )
        if deterministic is not None:
            self._record_interpretation("fallback", llm_success=False)
            return validate_intent(deterministic)
        self._record_interpretation("fallback", llm_success=False)
        raise ValueError(self._t("errors.vague_alert"))

    def _record_interpretation(self, method, *, llm_success):
        self.last_interpretation_method = method
        self.last_llm_success = llm_success
        logger.info(
            "alert_interpretation nlp_mode=%s interpretation_method=%s "
            "llm_success=%s llm_failure=%s",
            self.alert_nlp_mode,
            method,
            str(llm_success).lower(),
            str(method == "fallback").lower(),
        )

    def _handle_list_alerts(self):
        """Show every stored alert and remember them as the last ones shown."""
        rows = (
            self.repository.list_alert_rules(user_id=self.current_user_id)
            if self.repository is not None
            else []
        )
        self._remember_alerts([row[0] for row in rows])
        return self._format(AlertIntent(action="list"), rows)

    def _handle_delete_alert(self, text):
        """Delete the alert the sentence refers to, without extracting anything."""
        reference = read_alert_reference(text, "DELETE_ALERT")
        resolution = self._resolve(reference)
        if resolution.status == "unique":
            candidate = resolution.match
            self.repository.delete_alert_rule(
                candidate.rule_id, user_id=self.current_user_id
            )
            self._clear_remembered()
            return self._t("rules.deleted", label=self._alert_label(candidate))
        if resolution.status == "ambiguous":
            self._remember_alerts([item.rule_id for item in resolution.matches])
            return self._ambiguous_reply("eliminar", resolution.matches)
        return self._missing_alert_reply(resolution.status)

    def _handle_update_alert(self, text):
        """Change only the properties the sentence asks for, on the stored alert."""
        reference = read_alert_reference(text, "UPDATE_ALERT")
        if not reference.has_change:
            return self._t("rules.update_usage")
        resolution = self._resolve(reference)
        if resolution.status == "unique":
            candidate = resolution.match
            rule = updated_rule(candidate.rule, reference, legacy=candidate.legacy)
            try:
                self.repository.replace_alert_rule(
                    candidate.rule_id, rule, text, user_id=self.current_user_id
                )
            except ValueError as exc:
                return self._t("rules.update_error", error=exc)
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
        return (
            alert_candidates(self.repository, self.current_user_id)
            if self.repository is not None
            else ()
        )

    def _alert_label(self, candidate):
        return (
            f"#{candidate.rule_id} — {candidate.rule.query} — "
            f"{rule_price_text(candidate.rule, self.current_language)}"
        )

    def _ambiguous_reply(self, verb, candidates):
        listed = "\n".join(
            f"#{item.rule_id} — {item.rule.query} — {rule_price_text(item.rule, self.current_language)}"
            for item in candidates
        )
        example = self._t("rules.delete_example", id=candidates[0].rule_id)
        return self._t("rules.ambiguous", verb=verb, listed=listed, example=example)

    def _missing_alert_reply(self, status):
        if status == "context_required":
            return self._t("rules.context_required")
        return self._t("rules.not_found")

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
        for name in ("category_include", "category_exclude", "max_age_minutes"):
            if name in AlertIntent.model_fields:
                fields[name] = (
                    list(getattr(constraints, name, ()))
                    if name != "max_age_minutes"
                    else getattr(constraints, name)
                )
        return AlertIntent(**fields)

    def _remember_alerts(self, rule_ids):
        """Remember the alert(s) the bot just created, changed or showed."""
        if self.repository is None:
            return
        self.repository.set_alert_context(self.current_chat_id, rule_ids)

    def _remembered(self):
        if self.repository is None:
            return ()
        return self.repository.get_alert_context(self.current_chat_id)

    def _clear_remembered(self):
        if self.repository is None:
            return
        self.repository.clear_alert_context(self.current_chat_id)

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
            json={"chat_id": self.current_chat_id, "text": text},
            timeout=self.timeout,
            retries=self.retries,
            backoff=self.backoff,
            sleep=self._sleep,
        )

    def _format(self, intent, rows, baseline_count=None):
        if intent.action == "list":
            return format_alert_list(rows, self._stored_rule, self.current_language)
        verb = self._t(f"rules.action_{intent.action}")
        subject = (
            intent.query
            or intent.product_type
            or intent.brand
            or self._t("rules.any_deal")
        )
        if intent.action in {"create", "update"}:
            conditions = []
            if intent.max_price is not None:
                limit = f"{intent.max_price:.2f}".replace(".", ",")
                conditions.append(
                    self._t(
                        "rules.condition_price",
                        amount=limit,
                        currency=price_suffix(intent.price_unit),
                    )
                )
            temperature = temperature_condition(
                intent.temperature_min, intent.temperature_max, self.current_language
            )
            if temperature:
                # With a price, "… y al menos 250°" completes the sentence; on
                # its own, it needs the preposition the price condition gave it.
                conditions.append(
                    temperature
                    if conditions
                    else self._t("rules.with_temperature", value=temperature)
                )
            if intent.category_include:
                conditions.append(
                    self._t(
                        "rules.condition_category",
                        value=", ".join(intent.category_include),
                    )
                )
            if intent.category_exclude:
                conditions.append(
                    self._t(
                        "rules.condition_excluded_category",
                        value=", ".join(intent.category_exclude),
                    )
                )
            if intent.max_age_minutes is not None:
                conditions.append(
                    self._t(
                        "rules.condition_age",
                        value=format_number(intent.max_age_minutes),
                    )
                )
            reply = self._t(
                "rules.action_result", icon="✅", verb=verb, subject=subject
            )
            if conditions:
                reply += " " + self._t("rules.condition_join").join(conditions)
            elif intent.action == "create":
                reply += "\n" + self._t("rules.new_relevant")
            details = alert_detail_lines(intent, self.current_language)
            if details:
                reply += "\n" + "\n".join(details)
            if baseline_count is not None and intent.action == "create":
                reply += "\n" + self._t("rules.baseline", count=baseline_count)
            return reply
        icon = {"delete": "🗑️", "disable": "⏸️", "enable": "▶️"}[intent.action]
        return self._t("rules.action_result", icon=icon, verb=verb, subject=subject)

    def _listing_line(self, row):
        """One stored alert as Telegram lists it, read through its own rule.

        The canonical rule is what says which condition the alert really has:
        the legacy `max_price` column cannot tell an alert without a price from
        a price of zero, and a temperature-only alert has no price at all.
        """
        return format_listing_line(row, self._stored_rule(row))

    @staticmethod
    def _listing_summary(active, inactive):
        return listing_summary(active, inactive)

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
        return legacy_listing_price(row)
